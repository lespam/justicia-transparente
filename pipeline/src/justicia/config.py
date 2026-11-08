"""Configuración leída de variables de entorno."""

import os
from pathlib import Path

# User-Agent fijo e identificable (Bloque 1, sección 4.6). No se rota.
USER_AGENT = (
    "JusticiaTransparente/1.0 "
    "(+https://datalabitam.github.io/justicia-transparente; datalabitam@gmail.com)"
)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///justicia")
ALMACEN_DIR = Path(os.environ.get("ALMACEN_DIR", "datos"))
