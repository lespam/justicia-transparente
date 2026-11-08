"""Metadatos de una sentencia y su validación.

Cada adaptador llena `Metadatos` con reglas propias de su portal
(parse_metadata). Aquí vive lo común: la estructura, reglas generales sobre
el texto y la validación que decide entre 'indexada' y 'revision'.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

SENTIDOS = {
    "condena": "CONDENATORIO", "condenó": "CONDENATORIO", "condenatoria": "CONDENATORIO",
    "absuelve": "ABSOLUTORIO", "absolvió": "ABSOLUTORIO", "absolutoria": "ABSOLUTORIO",
}
_SENTIDO = re.compile(r"\bse\s+(condena|condenó|absuelve|absolvió)\b|\bsentencia\s+(condenatoria|absolutoria)\b",
                      re.IGNORECASE)
_TESTADO = re.compile(r"\*{5,}")


@dataclass
class Metadatos:
    materia: str
    fecha_emision: date | None = None
    fecha_publicacion: date | None = None
    expediente: str | None = None
    organo: str | None = None
    juez: str | None = None
    sentido_fallo: str | None = None
    delitos: list[str] = field(default_factory=list)
    descripcion: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def sentido_desde_texto(texto: str, limite: int = 3000) -> str | None:
    """CONDENATORIO o ABSOLUTORIO según la primera mención en el inicio."""
    m = _SENTIDO.search(texto[:limite])
    if not m:
        return None
    return SENTIDOS[(m.group(1) or m.group(2)).lower()]


def esta_testado(valor: str | None) -> bool:
    return bool(valor) and bool(_TESTADO.search(valor))


def problemas(m: Metadatos, texto: str) -> list[str]:
    """Razones para mandar la sentencia a 'revision'. Vacío = válida."""
    errores = []
    if m.fecha_emision is None:
        errores.append("sin fecha de emisión")
    if m.fecha_emision and m.fecha_publicacion and m.fecha_publicacion < m.fecha_emision:
        errores.append("publicación anterior a la emisión")
    if not m.sentido_fallo:
        errores.append("sin sentido del fallo")
    if not m.delitos and m.materia == "penal":
        errores.append("sin delitos")
    if len(texto) < 1000:
        errores.append("texto demasiado corto")
    return errores
