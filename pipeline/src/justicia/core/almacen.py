"""Almacenamiento de archivos por contenido (deduplicación nivel 2).

Cada archivo se guarda en raw/<2 primeros hex>/<sha256><ext>. Si dos URLs
entregan los mismos bytes, se guarda una sola copia.

AlmacenLocal sirve para desarrollo. En el Bloque 4 se agrega AlmacenS3 con la
misma interfaz para MinIO y S3.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ObjetoGuardado:
    sha256: bytes
    clave: str
    bytes: int
    nuevo: bool   # False si ya existía una copia idéntica


class Almacen(Protocol):
    async def guardar(self, datos: bytes, extension: str) -> ObjetoGuardado: ...
    async def leer(self, clave: str) -> bytes: ...


def clave_para(sha256_hex: str, extension: str) -> str:
    return f"raw/{sha256_hex[:2]}/{sha256_hex}{extension}"


class AlmacenLocal:
    def __init__(self, raiz: str | Path):
        self.raiz = Path(raiz)

    async def guardar(self, datos: bytes, extension: str) -> ObjetoGuardado:
        return await asyncio.to_thread(self._guardar, datos, extension)

    async def leer(self, clave: str) -> bytes:
        return await asyncio.to_thread((self.raiz / clave).read_bytes)

    def _guardar(self, datos: bytes, extension: str) -> ObjetoGuardado:
        sha = hashlib.sha256(datos)
        clave = clave_para(sha.hexdigest(), extension)
        ruta = self.raiz / clave
        nuevo = not ruta.exists()
        if nuevo:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            temporal = ruta.with_name(f"{ruta.name}.{os.getpid()}.tmp")
            temporal.write_bytes(datos)
            os.replace(temporal, ruta)   # atómico: nunca queda un archivo a medias
        return ObjetoGuardado(sha.digest(), clave, len(datos), nuevo)
