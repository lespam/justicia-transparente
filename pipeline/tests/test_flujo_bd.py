"""Descubrir y descargar contra PostgreSQL real y el portal falso.

Requiere TEST_ADMIN_DSN, una conexión con permiso para crear bases de datos:
    TEST_ADMIN_DSN=postgresql://postgres@localhost/postgres pytest
Cada prueba usa una base nueva con la migración 001 aplicada.
"""

import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from justicia.core.almacen import AlmacenLocal
from justicia.core.db import Repositorio
from justicia.flows.cosecha import descargar, descubrir

from .portal_nl import PortalFalso, Sentencia
from .test_unidades import Reloj

ADMIN = os.environ.get("TEST_ADMIN_DSN")
pytestmark = pytest.mark.skipif(not ADMIN, reason="TEST_ADMIN_DSN no definido")
ESQUEMA = Path(__file__).parents[1] / "sql" / "001_esquema_inicial.sql"


@pytest.fixture
async def repo():
    nombre = f"jt_prueba_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(ADMIN)
    await admin.execute(f"CREATE DATABASE {nombre}")
    dsn = ADMIN.rsplit("/", 1)[0] + f"/{nombre}"
    try:
        c = await asyncpg.connect(dsn)
        await c.execute(ESQUEMA.read_text())
        await c.close()
        r = await Repositorio.conectar(dsn)
        yield r
        await r.cerrar()
    finally:
        await admin.execute(f"DROP DATABASE {nombre} WITH (FORCE)")
        await admin.close()


def portal() -> PortalFalso:
    mismo_pdf = b"%PDF-1.7 contenido repetido"
    return PortalFalso(paginas=[
        [Sentencia("500", "06/noviembre/2026", "07/noviembre/2026", pdf=mismo_pdf),
         Sentencia("501", "05/noviembre/2026", "06/noviembre/2026", pdf=mismo_pdf)],
        [Sentencia("490", "20/octubre/2026", "30/octubre/2026", pdf=b"%PDF-1.7 otro")],
    ])


def falso(p: PortalFalso) -> dict:
    reloj = Reloj()
    return {"transport": p.transporte(), "dormir": reloj.dormir, "reloj": reloj}


async def test_ciclo_completo(repo, tmp_path):
    p = portal()

    r1 = await descubrir(repo, "nl-penal", **falso(p))
    assert r1 == {"estado": "exitosa", "nuevos": 3, "conocidos": 0, "repetidos": 0}
    fuente = await repo.obtener_fuente("nl-penal")
    assert fuente["marca_de_agua"].isoformat() == "2026-11-07"

    # Segunda ejecución: ventana desde el 31/oct. Lo conocido se salta sin clic.
    clics_antes = p.clics
    r2 = await descubrir(repo, "nl-penal", **falso(p))
    assert r2["nuevos"] == 0 and r2["conocidos"] == 2
    assert p.clics == clics_antes

    p.fallar_pdf = 4   # el primer PDF falla todos sus intentos
    r3 = await descargar(repo, "nl-penal", AlmacenLocal(tmp_path), **falso(p))
    assert r3 == {"estado": "exitosa", "descargada": 2, "duplicada": 0, "errores": 1}

    # El fallido se reprograma para más tarde, no se pierde.
    pendientes = await repo.pool.fetch(
        "SELECT intentos, ultimo_error FROM documento WHERE estado = 'descubierta'")
    assert len(pendientes) == 1 and pendientes[0]["intentos"] == 1
    assert "503" in pendientes[0]["ultimo_error"]
    assert await descargar(repo, "nl-penal", AlmacenLocal(tmp_path), **falso(p)) == {
        "estado": "exitosa", "descargada": 0, "duplicada": 0, "errores": 0}

    # Llega la hora del reintento: mismos bytes que otro documento → duplicada.
    await repo.pool.execute("UPDATE documento SET siguiente_intento = now()")
    r4 = await descargar(repo, "nl-penal", AlmacenLocal(tmp_path), **falso(p))
    assert r4 == {"estado": "exitosa", "descargada": 0, "duplicada": 1, "errores": 0}

    archivos = await repo.pool.fetchval("SELECT count(*) FROM archivo")
    assert archivos == 2                       # 3 documentos, 2 contenidos distintos
    assert len(list(tmp_path.rglob("*.pdf"))) == 2

    auditadas = await repo.pool.fetchval("SELECT count(*) FROM solicitud_http")
    assert auditadas == len(p.solicitudes)     # cada solicitud quedó registrada
    ejecuciones = await repo.pool.fetch("SELECT estado::text FROM ejecucion")
    assert {e["estado"] for e in ejecuciones} == {"exitosa"}


async def test_max_nuevos_no_mueve_la_marca(repo):
    r = await descubrir(repo, "nl-penal", max_nuevos=1, **falso(portal()))
    assert r["nuevos"] == 1
    fuente = await repo.obtener_fuente("nl-penal")
    assert fuente["marca_de_agua"] is None
