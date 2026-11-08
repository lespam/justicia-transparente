"""El adaptador de Nuevo León contra el portal falso."""

from datetime import date

from justicia.adapters.nuevo_leon import NuevoLeonPenal
from justicia.core.http import ClienteCortes

from .portal_nl import PortalFalso, Sentencia
from .test_unidades import UA, Reloj


def portal() -> PortalFalso:
    return PortalFalso(paginas=[
        [Sentencia("500", "06/noviembre/2026", "07/noviembre/2026", delito="ROBO"),
         Sentencia("500", "06/noviembre/2026", "07/noviembre/2026", delito="LESIONES"),
         Sentencia("499", "05/noviembre/2026", "06/noviembre/2026")],
        [Sentencia("490", "14/octubre/2026", "30/octubre/2026"),
         Sentencia("480", "10/octubre/2026", "12/octubre/2026")],
        [Sentencia("470", "01/octubre/2026", "02/octubre/2026")],
    ])


def nuevo_cliente(p: PortalFalso) -> ClienteCortes:
    reloj = Reloj()
    return ClienteCortes(UA, transport=p.transporte(), dormir=reloj.dormir, reloj=reloj)


async def test_discover_recorre_todas_las_paginas():
    p = portal()
    async with nuevo_cliente(p) as c:
        items = [i async for i in NuevoLeonPenal(c).discover(None)]
    assert [i.source_key for i in items] == ["500", "499", "490", "480", "470"]
    # Dos filas del registro 500 → un documento con ambas filas y un solo clic.
    assert [f["delitos"] for f in items[0].datos["filas"]] == ["ROBO", "LESIONES"]
    assert p.clics == 5
    assert items[0].fecha_publicacion == date(2026, 11, 7)
    assert items[0].datos["filas"][0]["fecha_emision_iso"] == "2026-11-06"
    assert items[0].datos["archivo_temporal"].startswith("500-")


async def test_discover_incremental_se_detiene():
    p = portal()
    async with nuevo_cliente(p) as c:
        items = [i async for i in NuevoLeonPenal(c).discover(date(2026, 10, 15))]
    # Página 2: "490" (publicada 30/oct) entra; "480" (12/oct) se ignora.
    # Toda la página 2 se emitió antes del 15/oct → no se pide la página 3.
    assert [i.source_key for i in items] == ["500", "499", "490"]
    posts = [s for s in p.solicitudes if s[0] == "POST"]
    assert len(posts) == 3 + 1   # 3 clics + cambio a la página 2


async def test_discover_salta_conocidos_sin_clic():
    p = portal()

    async def es_conocido(clave: str) -> bool:
        return clave in {"500", "490"}

    async with nuevo_cliente(p) as c:
        items = [i async for i in NuevoLeonPenal(c).discover(None, es_conocido=es_conocido)]
    assert [i.source_key for i in items] == ["499", "480", "470"]
    assert p.clics == 3


async def test_discover_max_paginas():
    p = portal()
    async with nuevo_cliente(p) as c:
        items = [i async for i in NuevoLeonPenal(c).discover(None, max_paginas=1)]
    assert len(items) == 2


async def test_fetch_sigue_la_redireccion():
    p = portal()
    async with nuevo_cliente(p) as c:
        adaptador = NuevoLeonPenal(c)
        item = await anext(adaptador.discover(None))
        crudo = await adaptador.fetch(item)
    assert crudo.contenido.startswith(b"%PDF")
    assert "controller.aspx?p=pdf-500-" in crudo.url_final
