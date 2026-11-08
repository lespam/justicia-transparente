"""Pruebas sin red ni base de datos."""

from datetime import date

import httpx
import pytest

from justicia.adapters.nuevo_leon import id_archivo, url_archivo
from justicia.core.almacen import AlmacenLocal
from justicia.core.fechas import parse_fecha
from justicia.core.http import BloqueadoPorRobots, CircuitoAbierto, ClienteCortes, ReintentosAgotados
from justicia.core.robots import ReglasRobots

from .portal_nl import ROBOTS

UA = "JusticiaTransparente/1.0 (+https://ejemplo)"


# -- Fechas -------------------------------------------------------------------

@pytest.mark.parametrize("texto, esperado", [
    ("18/marzo/2021", date(2021, 3, 18)),
    ("06/noviembre/2026", date(2026, 11, 6)),
    ("1 de Septiembre de 2020", date(2020, 9, 1)),
    ("1/setiembre/2020", date(2020, 9, 1)),
    ("18/03/2021", date(2021, 3, 18)),
    ("31/febrero/2021", None),
    ("", None),
    ("sin fecha", None),
])
def test_parse_fecha(texto, esperado):
    assert parse_fecha(texto) == esperado


# -- robots.txt -----------------------------------------------------------------

def test_robots_combina_grupos_asterisco():
    # urllib.robotparser solo leería el primer grupo y permitiría todo.
    reglas = ReglasRobots.desde_texto(ROBOTS, UA)
    assert reglas.permite("/SentenciasPublicas/Modulos/Penales.aspx")
    assert not reglas.permite("/consejojudicatura/RH/")


def test_robots_grupo_propio_y_regla_mas_larga():
    texto = """User-agent: *
Disallow: /

User-agent: JusticiaTransparente
Disallow: /privado/
Allow: /privado/publico$
"""
    reglas = ReglasRobots.desde_texto(texto, UA)
    assert reglas.permite("/sentencias")            # el grupo propio reemplaza a "*"
    assert not reglas.permite("/privado/x")
    assert reglas.permite("/privado/publico")
    assert not reglas.permite("/privado/publico/otro")


# -- Gateway de cortesía ----------------------------------------------------------

class Reloj:
    """Reloj falso: dormir() avanza el tiempo sin esperar."""

    def __init__(self):
        self.t = 0.0
        self.esperas: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def dormir(self, s: float) -> None:
        self.esperas.append(s)
        self.t += s


def cliente(manejador, reloj, auditoria=None, **kw):
    async def auditor(r):
        if auditoria is not None:
            auditoria.append(r)
    return ClienteCortes(UA, transport=httpx.MockTransport(manejador), auditor=auditor,
                         dormir=reloj.dormir, reloj=reloj, **kw)


def con_robots(resto):
    def manejador(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return resto(req)
    return manejador


async def test_intervalo_entre_solicitudes():
    reloj = Reloj()
    async with cliente(con_robots(lambda r: httpx.Response(200)), reloj) as c:
        await c.get("https://h.mx/a")
        await c.get("https://h.mx/b")
    # robots.txt, /a y /b: dos esperas, cada una entre 2 y 5 s
    assert len(reloj.esperas) == 2
    assert all(2 <= e <= 5 for e in reloj.esperas)


async def test_reintenta_y_respeta_retry_after():
    respuestas = iter([httpx.Response(503, headers={"Retry-After": "42"}), httpx.Response(200)])
    reloj, auditoria = Reloj(), []
    async with cliente(con_robots(lambda r: next(respuestas)), reloj, auditoria) as c:
        r = await c.get("https://h.mx/a")
    assert r.status_code == 200
    assert 42 in reloj.esperas
    assert [a.estado_http for a in auditoria] == [200, 503, 200]


async def test_robots_bloquea_y_audita():
    reloj, auditoria = Reloj(), []
    async with cliente(con_robots(lambda r: httpx.Response(200)), reloj, auditoria) as c:
        with pytest.raises(BloqueadoPorRobots):
            await c.get("https://h.mx/consejojudicatura/x")
    assert auditoria[-1].permitida_robots is False


async def test_robots_inalcanzable_bloquea_todo():
    reloj = Reloj()
    async with cliente(lambda r: httpx.Response(503), reloj, max_intentos=2) as c:
        with pytest.raises(BloqueadoPorRobots):
            await c.get("https://h.mx/a")


async def test_404_no_se_reintenta():
    reloj, auditoria = Reloj(), []
    async with cliente(con_robots(lambda r: httpx.Response(404)), reloj, auditoria) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get("https://h.mx/a")
    assert len(auditoria) == 2   # robots.txt + un solo intento


async def test_circuit_breaker():
    reloj = Reloj()
    async with cliente(con_robots(lambda r: httpx.Response(500)), reloj,
                       max_intentos=2, umbral_circuito=2, enfriamiento_circuito=600) as c:
        for _ in range(2):
            with pytest.raises(ReintentosAgotados):
                await c.get("https://h.mx/a")
        with pytest.raises(CircuitoAbierto):
            await c.get("https://h.mx/a")
        assert c.circuito_abierto_hasta("h.mx") is not None


# -- Almacenamiento ---------------------------------------------------------------

async def test_almacen_por_contenido(tmp_path):
    almacen = AlmacenLocal(tmp_path)
    a = await almacen.guardar(b"%PDF uno", ".pdf")
    b = await almacen.guardar(b"%PDF uno", ".pdf")
    assert a.nuevo and not b.nuevo
    assert a.clave == b.clave == f"raw/{a.sha256.hex()[:2]}/{a.sha256.hex()}.pdf"
    assert await almacen.leer(a.clave) == b"%PDF uno"


# -- Nuevo León: lectura de HTML y CSV ----------------------------------------------

def test_url_archivo_desescapa_diagonales():
    visor = "var DEFAULT_URL = 'controller.aspx?file=~\\\\files\\\\489347-3b3ac8e5-08fe-4450-906f-b66067e7a69b.pdf';"
    url = url_archivo(visor)
    assert url.endswith("controller.aspx?file=~\\files\\489347-3b3ac8e5-08fe-4450-906f-b66067e7a69b.pdf")
    assert id_archivo(url) == "489347-3b3ac8e5-08fe-4450-906f-b66067e7a69b"
