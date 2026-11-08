"""Acceso a PostgreSQL. Todo el SQL del pipeline de cosecha está aquí."""

from __future__ import annotations

import json
from datetime import date, datetime
from uuid import UUID

import asyncpg

from ..adapters.base import ListingItem
from ..processing.metadata import Metadatos
from .almacen import ObjetoGuardado
from .http import RegistroSolicitud


async def _configurar(conexion: asyncpg.Connection) -> None:
    for tipo in ("json", "jsonb"):
        await conexion.set_type_codec(
            tipo, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Repositorio:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def conectar(cls, dsn: str) -> Repositorio:
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, init=_configurar,
            server_settings={"search_path": "justicia,public"})
        return cls(pool)

    async def cerrar(self) -> None:
        await self.pool.close()

    # -- Fuentes y ejecuciones ------------------------------------------------

    async def obtener_fuente(self, fuente_id: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM fuente WHERE id = $1", fuente_id)

    async def avanzar_marca_de_agua(self, fuente_id: str, fecha: date) -> None:
        # GREATEST ignora NULL: la primera vez toma `fecha`.
        await self.pool.execute(
            "UPDATE fuente SET marca_de_agua = GREATEST(marca_de_agua, $2) WHERE id = $1",
            fuente_id, fecha)

    async def abrir_circuito(self, fuente_id: str, hasta: datetime) -> None:
        await self.pool.execute(
            "UPDATE fuente SET circuito_abierto_hasta = $2 WHERE id = $1", fuente_id, hasta)

    async def iniciar_ejecucion(self, fuente_id: str, etapa: str) -> UUID:
        return await self.pool.fetchval(
            "INSERT INTO ejecucion (fuente_id, etapa) VALUES ($1, $2) RETURNING id",
            fuente_id, etapa)

    async def terminar_ejecucion(self, ejecucion_id: UUID, estado: str,
                                 contadores: dict, error: str | None = None) -> None:
        await self.pool.execute(
            """UPDATE ejecucion
                  SET estado = $2, terminada_en = now(), contadores = $3, error = $4
                WHERE id = $1""",
            ejecucion_id, estado, contadores, error)

    async def registrar_solicitud(self, ejecucion_id: UUID, fuente_id: str,
                                  r: RegistroSolicitud) -> None:
        await self.pool.execute(
            """INSERT INTO solicitud_http
                   (ejecucion_id, fuente_id, metodo, url, permitida_robots,
                    estado_http, bytes, sha256, duracion_ms, error)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            ejecucion_id, fuente_id, r.metodo, r.url, r.permitida_robots,
            r.estado_http, r.bytes, r.sha256, r.duracion_ms, r.error)

    # -- Documentos -----------------------------------------------------------

    async def documento_existe(self, fuente_id: str, source_key: str) -> bool:
        return await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM documento WHERE fuente_id = $1 AND source_key = $2)",
            fuente_id, source_key)

    async def registrar_documento(self, fuente_id: str, item: ListingItem) -> bool:
        """Deduplicación nivel 1. Devuelve True si el documento es nuevo."""
        nuevo = await self.pool.fetchval(
            """INSERT INTO documento (fuente_id, source_key, url, datos_listado,
                                      fecha_publicacion)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (fuente_id, source_key) DO NOTHING
               RETURNING id""",
            fuente_id, item.source_key, item.url, item.datos, item.fecha_publicacion)
        return nuevo is not None

    async def tomar(self, estado: str, limite: int, fuente_id: str,
                    lease_min: int = 15) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM tomar_documentos($1, $2, make_interval(mins => $3), $4)",
            estado, limite, lease_min, fuente_id)

    async def guardar_descarga(self, documento_id: int, obj: ObjetoGuardado,
                               tipo_mime: str) -> str:
        """Registra el archivo y avanza el documento. Devuelve el estado nuevo.

        Deduplicación nivel 2: si otro documento ya tiene los mismos bytes,
        este queda como 'duplicada' y hereda su sentencia (si ya existe).
        """
        async with self.pool.acquire() as c, c.transaction():
            await c.execute(
                """INSERT INTO archivo (sha256, bytes, tipo_mime, clave_objeto)
                   VALUES ($1, $2, $3, $4) ON CONFLICT (sha256) DO NOTHING""",
                obj.sha256, obj.bytes, tipo_mime, obj.clave)
            otro = await c.fetchrow(
                """SELECT id, sentencia_id FROM documento
                    WHERE archivo_sha256 = $1 AND id <> $2
                    ORDER BY sentencia_id NULLS LAST LIMIT 1""",
                obj.sha256, documento_id)
            estado = "duplicada" if otro else "descargada"
            await c.execute(
                """UPDATE documento
                      SET estado = $3, archivo_sha256 = $2, sentencia_id = $4,
                          bloqueado_hasta = NULL, ultimo_error = NULL
                    WHERE id = $1""",
                documento_id, obj.sha256, estado, otro["sentencia_id"] if otro else None)
            return estado

    async def registrar_fallo(self, documento_id: int, error: str,
                              max_intentos: int, base_min: int = 30) -> str:
        """Programa el siguiente intento (30 min, 1 h, 2 h, …) o marca 'fallida'."""
        return await self.pool.fetchval(
            """UPDATE documento
                  SET ultimo_error = $2,
                      bloqueado_hasta = NULL,
                      estado = CASE WHEN intentos >= $3 THEN 'fallida'::estado_documento
                                    ELSE estado END,
                      siguiente_intento = now() + make_interval(
                          mins => $4 * (2 ^ LEAST(intentos - 1, 6))::int)
                WHERE id = $1
            RETURNING estado::text""",
            documento_id, error[:2000], max_intentos, base_min)

    async def liberar(self, documento_id: int) -> None:
        """Devuelve un documento a la cola sin contar el intento."""
        await self.pool.execute(
            """UPDATE documento
                  SET bloqueado_hasta = NULL, intentos = GREATEST(intentos - 1, 0)
                WHERE id = $1""",
            documento_id)

    # -- Procesamiento (etapa 3) ---------------------------------------------

    async def clave_de_archivo(self, sha256: bytes) -> str:
        return await self.pool.fetchval(
            "SELECT clave_objeto FROM archivo WHERE sha256 = $1", sha256)

    async def sentencia_por_texto(self, texto_sha256: bytes) -> int | None:
        return await self.pool.fetchval(
            "SELECT id FROM sentencia WHERE texto_sha256 = $1", texto_sha256)

    async def marcar_documento(self, documento_id: int, estado: str, *,
                               sentencia_id: int | None = None,
                               motivo: str | None = None) -> None:
        await self.pool.execute(
            """UPDATE documento
                  SET estado = $2, sentencia_id = COALESCE($3, sentencia_id),
                      ultimo_error = $4, bloqueado_hasta = NULL
                WHERE id = $1""",
            documento_id, estado, sentencia_id, motivo)

    async def guardar_sentencia(self, documento_id: int, fuente_id: str,
                                archivo_sha256: bytes, texto_sha256: bytes,
                                m: Metadatos, texto_publico: str, texto_original: str,
                                num_paginas: int, uso_ocr: bool,
                                extractor_version: str, metadatos_version: str
                                ) -> tuple[str, int]:
        """Inserta la sentencia y su texto privado; marca el documento.

        Devuelve ('indexada', id) o, si otro proceso guardó el mismo texto
        primero, ('duplicada', id_existente). Todo en una transacción.
        """
        async with self.pool.acquire() as c, c.transaction():
            sentencia_id = await c.fetchval(
                """INSERT INTO sentencia (
                       fuente_id, archivo_sha256, texto_sha256, expediente, organo,
                       juez, materia, fecha_emision, fecha_publicacion, sentido_fallo,
                       delitos, descripcion, metadatos_extra, texto_publico,
                       num_paginas, uso_ocr, extractor_version, metadatos_version)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                           $14, $15, $16, $17, $18)
                   ON CONFLICT (texto_sha256) DO NOTHING
                   RETURNING id""",
                fuente_id, archivo_sha256, texto_sha256, m.expediente, m.organo,
                m.juez, m.materia, m.fecha_emision, m.fecha_publicacion,
                m.sentido_fallo, m.delitos, m.descripcion, m.extra, texto_publico,
                num_paginas, uso_ocr, extractor_version, metadatos_version)
            if sentencia_id is None:
                estado = "duplicada"
                sentencia_id = await c.fetchval(
                    "SELECT id FROM sentencia WHERE texto_sha256 = $1", texto_sha256)
            else:
                estado = "indexada"
                await c.execute(
                    "INSERT INTO privado.texto_original (sentencia_id, texto) VALUES ($1, $2)",
                    sentencia_id, texto_original)
            await c.execute(
                """UPDATE documento
                      SET estado = $2, sentencia_id = $3, ultimo_error = NULL,
                          bloqueado_hasta = NULL
                    WHERE id = $1""",
                documento_id, estado, sentencia_id)
            return estado, sentencia_id

    async def resumen(self, fuente_id: str) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT estado::text, count(*) AS total FROM documento
                WHERE fuente_id = $1 GROUP BY estado ORDER BY estado""",
            fuente_id)
