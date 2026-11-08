"""Contrato común de los adaptadores por estado (Bloque 1, sección 4.1)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol


@dataclass(frozen=True)
class ListingItem:
    source_key: str                 # llave estable para deduplicación nivel 1
    url: str                        # URL directa del archivo
    fecha_publicacion: date | None
    datos: dict[str, Any] = field(default_factory=dict)   # fila del listado


@dataclass(frozen=True)
class RawDocument:
    contenido: bytes
    tipo_mime: str
    url_final: str


class DocumentoInvalido(Exception):
    """El portal respondió, pero no entregó un documento válido."""


class SourceAdapter(Protocol):
    fuente: str

    def discover(
        self,
        desde: date | None,
        *,
        es_conocido: Callable[[str], Awaitable[bool]] | None = None,
        max_paginas: int | None = None,
    ) -> AsyncIterator[ListingItem]:
        """Items publicados desde `desde`, del más reciente al más antiguo.

        - Con `desde=None` (primera ejecución) recorre hasta `max_paginas`.
        - `es_conocido(source_key)` permite saltar lo ya registrado antes de
          gastar solicitudes en resolver la URL del archivo.
        """
        ...

    async def fetch(self, item: ListingItem) -> RawDocument: ...

    # parse_metadata() se agrega en el Bloque 3b (extracción de metadatos).
