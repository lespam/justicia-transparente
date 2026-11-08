"""Registro de adaptadores: un id de fuente → una clase."""

from ..core.http import ClienteCortes
from .base import SourceAdapter
from .nuevo_leon import NuevoLeonPenal

ADAPTADORES = {
    NuevoLeonPenal.fuente: NuevoLeonPenal,
}


def crear_adaptador(fuente_id: str, cliente: ClienteCortes) -> SourceAdapter:
    try:
        return ADAPTADORES[fuente_id](cliente)
    except KeyError:
        raise ValueError(f"no hay adaptador para la fuente {fuente_id!r}") from None
