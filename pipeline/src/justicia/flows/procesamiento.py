"""Etapa 3 del pipeline: texto, metadatos, enmascaramiento e índice.

Toma documentos 'descargada' y los deja en uno de estos estados:
- 'indexada':  sentencia guardada; la búsqueda por palabras ya la encuentra.
- 'duplicada': el mismo texto ya existe (deduplicación nivel 3).
- 'revision':  faltan metadatos obligatorios o el PDF necesita OCR.
               El motivo queda en documento.ultimo_error.

Con `estado="revision"` se reprocesan los documentos en revisión, por ejemplo
después de corregir una regla.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from ..adapters import crear_adaptador
from ..adapters.base import ListingItem
from ..core.almacen import Almacen
from ..core.db import Repositorio
from ..processing.extract import EXTRACTOR_VERSION, extraer_pdf
from ..processing.metadata import problemas
from ..processing.pii import PII_VERSION, enmascarar

log = structlog.get_logger()


async def procesar(repo: Repositorio, fuente_id: str, almacen: Almacen, *,
                   limite: int = 50, lote: int = 5, estado: str = "descargada",
                   max_intentos: int = 3) -> dict[str, Any]:
    ejecucion = await repo.iniciar_ejecucion(fuente_id, "procesar")
    structlog.contextvars.bind_contextvars(run_id=str(ejecucion), fuente=fuente_id)
    # El procesamiento no hace solicitudes HTTP: el adaptador no necesita cliente.
    adaptador = crear_adaptador(fuente_id, cliente=None)
    cont = {"indexada": 0, "duplicada": 0, "revision": 0, "errores": 0}

    procesados = 0
    while procesados < limite:
        docs = await repo.tomar(estado, min(lote, limite - procesados), fuente_id)
        if not docs:
            break
        for doc in docs:
            procesados += 1
            try:
                resultado = await _procesar_uno(repo, adaptador, almacen, fuente_id, doc)
            except Exception as e:   # PDF dañado, error de la librería, etc.
                cont["errores"] += 1
                await repo.registrar_fallo(doc["id"], f"{type(e).__name__}: {e}", max_intentos)
                log.exception("procesar_fallido", documento=doc["id"])
                continue
            cont[resultado] += 1

    await repo.terminar_ejecucion(ejecucion, "exitosa", cont)
    log.info("procesar_fin", **cont)
    structlog.contextvars.clear_contextvars()
    return {"estado": "exitosa", **cont}


async def _procesar_uno(repo: Repositorio, adaptador, almacen: Almacen,
                        fuente_id: str, doc) -> str:
    clave = await repo.clave_de_archivo(doc["archivo_sha256"])
    datos = await almacen.leer(clave)
    # pdfium y spaCy usan CPU: se corren fuera del event loop.
    extraido = await asyncio.to_thread(extraer_pdf, datos)
    bitacora = log.bind(documento=doc["id"], source_key=doc["source_key"])

    if extraido.necesita_ocr:
        await repo.marcar_documento(doc["id"], "revision", motivo="requiere OCR")
        bitacora.info("documento_revision", motivo="requiere OCR")
        return "revision"

    existente = await repo.sentencia_por_texto(extraido.sha256)
    if existente is not None:
        await repo.marcar_documento(doc["id"], "duplicada", sentencia_id=existente)
        bitacora.info("documento_duplicado", sentencia_id=existente)
        return "duplicada"

    datos_listado = doc["datos_listado"]
    if isinstance(datos_listado, str):
        datos_listado = json.loads(datos_listado)
    item = ListingItem(doc["source_key"], doc["url"], doc["fecha_publicacion"], datos_listado)
    meta = adaptador.parse_metadata(extraido.texto, item)
    errores = problemas(meta, extraido.texto)
    if errores:
        motivo = "; ".join(errores)
        await repo.marcar_documento(doc["id"], "revision", motivo=motivo)
        bitacora.info("documento_revision", motivo=motivo)
        return "revision"

    publico = await asyncio.to_thread(
        enmascarar, extraido.texto, [meta.juez] if meta.juez else [])
    meta.extra["enmascarado"] = {"version": PII_VERSION, "conteos": dict(publico.conteos)}

    estado, sentencia_id = await repo.guardar_sentencia(
        doc["id"], fuente_id, doc["archivo_sha256"], extraido.sha256, meta,
        publico.texto, extraido.texto, extraido.num_paginas, uso_ocr=False,
        extractor_version=EXTRACTOR_VERSION,
        metadatos_version=adaptador.metadatos_version)
    bitacora.info("documento_procesado", estado=estado, sentencia_id=sentencia_id,
                  paginas=extraido.num_paginas, tapados=sum(publico.conteos.values()))
    return estado
