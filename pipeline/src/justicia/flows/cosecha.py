"""Etapas 1 y 2 del pipeline: descubrir y descargar.

Son funciones async normales. En el Bloque 4 Prefect las envuelve como tareas
programadas; aquí se pueden correr desde la línea de comandos.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from ..adapters import crear_adaptador
from ..adapters.base import DocumentoInvalido, ListingItem
from ..config import USER_AGENT
from ..core.almacen import Almacen
from ..core.db import Repositorio
from ..core.http import CircuitoAbierto, ClienteCortes, ErrorCosecha, RegistroSolicitud

log = structlog.get_logger()

EXTENSIONES = {"application/pdf": ".pdf"}


class FuenteNoDisponible(Exception):
    pass


async def _preparar(repo: Repositorio, fuente_id: str, etapa: str):
    fuente = await repo.obtener_fuente(fuente_id)
    if fuente is None:
        raise FuenteNoDisponible(f"la fuente {fuente_id!r} no existe")
    if not fuente["activa"]:
        raise FuenteNoDisponible(f"la fuente {fuente_id!r} está inactiva")
    abierto = fuente["circuito_abierto_hasta"]
    if abierto and abierto > datetime.now(timezone.utc):
        raise FuenteNoDisponible(f"circuito abierto hasta {abierto.isoformat()}")

    ejecucion = await repo.iniciar_ejecucion(fuente_id, etapa)
    structlog.contextvars.bind_contextvars(run_id=str(ejecucion), fuente=fuente_id)

    async def auditor(registro: RegistroSolicitud) -> None:
        await repo.registrar_solicitud(ejecucion, fuente_id, registro)

    def cliente(**extra: Any) -> ClienteCortes:
        intervalo = (float(fuente["intervalo_min_s"]), float(fuente["intervalo_max_s"]))
        return ClienteCortes(USER_AGENT, intervalo=intervalo, auditor=auditor, **extra)

    return fuente, ejecucion, cliente


async def _abrir_circuito(repo: Repositorio, fuente_id: str, cliente: ClienteCortes,
                          error: CircuitoAbierto) -> None:
    hasta = cliente.circuito_abierto_hasta(error.host)
    if hasta:
        await repo.abrir_circuito(fuente_id, hasta)
    log.warning("circuito_abierto", host=error.host, hasta=hasta and hasta.isoformat())


async def descubrir(repo: Repositorio, fuente_id: str, *, max_paginas: int | None = None,
                    max_nuevos: int | None = None, **cliente_extra: Any) -> dict:
    """Etapa 1: registra en `documento` los items nuevos del listado.

    La marca de agua solo avanza si la ejecución termina bien. El listado va de
    lo más reciente a lo más antiguo; si avanzara a mitad de camino, una falla
    dejaría huecos que la siguiente ejecución ya no revisaría.
    """
    fuente, ejecucion, nuevo_cliente = await _preparar(repo, fuente_id, "descubrir")
    marca = fuente["marca_de_agua"]
    desde = marca - timedelta(days=fuente["ventana_dias"]) if marca else None
    # conocidos: saltados sin clic. repetidos: resueltos, pero ya registrados
    # por otra ejecución simultánea (raro).
    cont = {"nuevos": 0, "conocidos": 0, "repetidos": 0}
    mas_reciente: date | None = None
    log.info("descubrir_inicio", desde=desde and desde.isoformat())

    estado, error = "exitosa", None
    async with nuevo_cliente(**cliente_extra) as cliente:
        adaptador = crear_adaptador(fuente_id, cliente)
        async def es_conocido(source_key: str) -> bool:
            conocido = await repo.documento_existe(fuente_id, source_key)
            cont["conocidos"] += conocido
            return conocido

        items = adaptador.discover(desde, es_conocido=es_conocido, max_paginas=max_paginas)
        try:
            async for item in items:
                if await repo.registrar_documento(fuente_id, item):
                    cont["nuevos"] += 1
                    log.info("documento_nuevo", source_key=item.source_key,
                             publicada=item.fecha_publicacion and item.fecha_publicacion.isoformat())
                else:
                    cont["repetidos"] += 1
                if item.fecha_publicacion:
                    mas_reciente = max(mas_reciente or item.fecha_publicacion,
                                       item.fecha_publicacion)
                if max_nuevos and cont["nuevos"] >= max_nuevos:
                    break
        except CircuitoAbierto as e:
            await _abrir_circuito(repo, fuente_id, cliente, e)
            estado, error = "fallida", str(e)
        except (ErrorCosecha, DocumentoInvalido, httpx.HTTPError) as e:
            estado, error = "fallida", f"{type(e).__name__}: {e}"
        finally:
            await items.aclose()

    # Un recorrido cortado por max_nuevos/max_paginas no garantiza haber visto
    # toda la ventana, así que tampoco mueve la marca de agua.
    completo = estado == "exitosa" and not max_nuevos and not max_paginas
    if completo and mas_reciente:
        await repo.avanzar_marca_de_agua(fuente_id, mas_reciente)
    await repo.terminar_ejecucion(ejecucion, estado, cont, error)
    log.info("descubrir_fin", estado=estado, error=error, **cont)
    structlog.contextvars.clear_contextvars()
    return {"estado": estado, **cont}


async def descargar(repo: Repositorio, fuente_id: str, almacen: Almacen, *,
                    limite: int = 50, lote: int = 5, max_intentos: int = 5,
                    **cliente_extra: Any) -> dict:
    """Etapa 2: descarga documentos 'descubierta' y los guarda por hash."""
    _, ejecucion, nuevo_cliente = await _preparar(repo, fuente_id, "descargar")
    cont = {"descargada": 0, "duplicada": 0, "errores": 0}
    estado, error = "exitosa", None

    async with nuevo_cliente(**cliente_extra) as cliente:
        adaptador = crear_adaptador(fuente_id, cliente)
        procesados = 0
        while procesados < limite and estado == "exitosa":
            # Lotes pequeños: el lease de 15 min no vence a mitad del lote.
            docs = await repo.tomar("descubierta", min(lote, limite - procesados), fuente_id)
            if not docs:
                break
            for i, doc in enumerate(docs):
                procesados += 1
                item = ListingItem(doc["source_key"], doc["url"],
                                   doc["fecha_publicacion"], doc["datos_listado"])
                try:
                    crudo = await adaptador.fetch(item)
                except CircuitoAbierto as e:
                    for pendiente in docs[i:]:
                        await repo.liberar(pendiente["id"])
                    await _abrir_circuito(repo, fuente_id, cliente, e)
                    estado, error = "fallida", str(e)
                    break
                except (ErrorCosecha, DocumentoInvalido, httpx.HTTPError) as e:
                    cont["errores"] += 1
                    nuevo = await repo.registrar_fallo(
                        doc["id"], f"{type(e).__name__}: {e}", max_intentos)
                    log.warning("descarga_fallida", source_key=item.source_key,
                                error=str(e), estado=nuevo)
                    continue

                ext = EXTENSIONES.get(crudo.tipo_mime, "")
                obj = await almacen.guardar(crudo.contenido, ext)
                resultado = await repo.guardar_descarga(doc["id"], obj, crudo.tipo_mime)
                cont[resultado] += 1
                log.info("documento_descargado", source_key=item.source_key,
                         estado=resultado, bytes=obj.bytes, sha256=obj.sha256.hex()[:16],
                         host=urlsplit(crudo.url_final).netloc)

    await repo.terminar_ejecucion(ejecucion, estado, cont, error)
    log.info("descargar_fin", estado=estado, error=error, **cont)
    structlog.contextvars.clear_contextvars()
    return {"estado": estado, **cont}
