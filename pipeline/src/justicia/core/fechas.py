"""Normalización de fechas escritas en español."""

import re
import unicodedata
from datetime import date

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

_TEXTUAL = re.compile(
    r"(\d{1,2})\s*(?:/|-|\s)\s*(?:de\s+)?([a-z]+)\s*(?:/|-|\s)\s*(?:del?\s+)?(\d{4})"
)
_NUMERICA = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _sin_acentos(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def parse_fecha(texto: str | None) -> date | None:
    """Convierte "18/marzo/2021", "18 de marzo de 2021" o "18/03/2021" en date.

    Devuelve None si el texto está vacío o no es una fecha válida.
    """
    if not texto:
        return None
    limpio = _sin_acentos(texto.strip().lower())
    try:
        if m := _TEXTUAL.fullmatch(limpio):
            mes = MESES.get(m[2])
            return date(int(m[3]), mes, int(m[1])) if mes else None
        if m := _NUMERICA.fullmatch(limpio):
            return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:  # p. ej. 31/febrero
        return None
    return None
