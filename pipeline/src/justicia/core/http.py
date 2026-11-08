"""Gateway de cortesía: toda solicitud a un portal pasa por aquí.

Aplica la política de cosecha responsable del Bloque 1 (sección 4.6):
1. robots.txt se descarga una vez por ejecución y se respeta.
2. Una solicitud a la vez por host, con 2–5 s aleatorios entre solicitudes.
3. Reintentos con backoff exponencial y variación; respeta Retry-After.
4. Circuit breaker: tras varios fallos seguidos se deja de consultar el host.
5. Cada solicitud se reporta al auditor (tabla solicitud_http).
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .robots import ReglasRobots

REINTENTABLES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RegistroSolicitud:
    metodo: str
    url: str
    permitida_robots: bool
    estado_http: int | None = None
    bytes: int | None = None
    sha256: bytes | None = None
    duracion_ms: int | None = None
    error: str | None = None


Auditor = Callable[[RegistroSolicitud], Awaitable[None]]


class ErrorCosecha(Exception):
    """Base de los errores del gateway."""


class BloqueadoPorRobots(ErrorCosecha):
    pass


class ReintentosAgotados(ErrorCosecha):
    pass


class CircuitoAbierto(ErrorCosecha):
    def __init__(self, host: str, segundos_restantes: float):
        super().__init__(f"circuito abierto para {host} ({segundos_restantes:.0f} s)")
        self.host = host
        self.segundos_restantes = segundos_restantes


class ClienteCortes:
    def __init__(
        self,
        user_agent: str,
        *,
        intervalo: tuple[float, float] = (2.0, 5.0),
        max_intentos: int = 4,
        backoff_base: float = 4.0,
        backoff_max: float = 300.0,
        timeout: float = 90.0,
        umbral_circuito: int = 3,
        enfriamiento_circuito: float = 900.0,
        auditor: Auditor | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
        reloj: Callable[[], float] = time.monotonic,
        azar: random.Random | None = None,
    ):
        self.user_agent = user_agent
        self.intervalo = intervalo
        self.max_intentos = max_intentos
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.umbral_circuito = umbral_circuito
        self.enfriamiento_circuito = enfriamiento_circuito
        self._auditor = auditor
        self._dormir = dormir
        self._reloj = reloj
        self._azar = azar or random.Random()
        self._http = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._ultimo: dict[str, float] = {}
        self._robots: dict[str, ReglasRobots] = {}
        self._fallos: dict[str, int] = defaultdict(int)
        self._abierto_hasta: dict[str, float] = {}

    async def __aenter__(self) -> ClienteCortes:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    # -- API pública ---------------------------------------------------------

    async def get(self, url: str, **kw) -> httpx.Response:
        return await self.solicitar("GET", url, **kw)

    async def post(self, url: str, **kw) -> httpx.Response:
        return await self.solicitar("POST", url, **kw)

    async def solicitar(self, metodo: str, url: str, **kw) -> httpx.Response:
        partes = urlsplit(url)
        host = partes.netloc
        self._verificar_circuito(host)
        reglas = await self._reglas(partes.scheme, host)
        ruta = partes.path + (f"?{partes.query}" if partes.query else "")
        if not reglas.permite(ruta or "/"):
            await self._auditar(RegistroSolicitud(
                metodo, url, permitida_robots=False, error="bloqueada por robots.txt"))
            raise BloqueadoPorRobots(url)
        return await self._enviar(metodo, url, host, **kw)

    def circuito_abierto_hasta(self, host: str) -> datetime | None:
        """Hora (UTC) en que se cierra el circuito, para guardarla en la BD."""
        hasta = self._abierto_hasta.get(host)
        if hasta is None:
            return None
        restante = max(0.0, hasta - self._reloj())
        return datetime.fromtimestamp(time.time() + restante, tz=timezone.utc)

    # -- Internos ------------------------------------------------------------

    async def _reglas(self, esquema: str, host: str) -> ReglasRobots:
        if host not in self._robots:
            url = f"{esquema}://{host}/robots.txt"
            try:
                r = await self._enviar("GET", url, host)
                reglas = ReglasRobots.desde_texto(r.text, self.user_agent)
            except httpx.HTTPStatusError as e:
                # RFC 9309: 4xx = no hay restricciones.
                if 400 <= e.response.status_code < 500:
                    reglas = ReglasRobots.permitir_todo()
                else:
                    raise
            except ReintentosAgotados:
                # RFC 9309: inalcanzable = asumir que todo está bloqueado.
                reglas = ReglasRobots.bloquear_todo()
            self._robots[host] = reglas
        return self._robots[host]

    def _verificar_circuito(self, host: str) -> None:
        hasta = self._abierto_hasta.get(host)
        if hasta is not None:
            restante = hasta - self._reloj()
            if restante > 0:
                raise CircuitoAbierto(host, restante)
            del self._abierto_hasta[host]
            self._fallos[host] = 0

    async def _esperar_turno(self, host: str) -> None:
        anterior = self._ultimo.get(host)
        if anterior is not None:
            objetivo = anterior + self._azar.uniform(*self.intervalo)
            espera = objetivo - self._reloj()
            if espera > 0:
                await self._dormir(espera)
        self._ultimo[host] = self._reloj()

    def _backoff(self, intento: int, retry_after: str | None) -> float:
        if retry_after:
            segundos = _segundos_retry_after(retry_after)
            if segundos is not None:
                return min(self.backoff_max, segundos)
        base = self.backoff_base * 2 ** (intento - 1)
        return min(self.backoff_max, base + self._azar.uniform(0, self.backoff_base))

    async def _enviar(self, metodo: str, url: str, host: str, **kw) -> httpx.Response:
        ultimo_error = ""
        async with self._candados[host]:
            for intento in range(1, self.max_intentos + 1):
                await self._esperar_turno(host)
                inicio = self._reloj()
                retry_after = None
                try:
                    r = await self._http.request(metodo, url, **kw)
                except httpx.TransportError as e:
                    ultimo_error = f"{type(e).__name__}: {e}"
                    await self._auditar(RegistroSolicitud(
                        metodo, url, True, duracion_ms=_ms(inicio, self._reloj()),
                        error=ultimo_error))
                else:
                    for salto in r.history:   # redirecciones seguidas por httpx
                        await self._auditar(RegistroSolicitud(
                            salto.request.method, str(salto.url), True,
                            estado_http=salto.status_code, bytes=len(salto.content)))
                    await self._auditar(RegistroSolicitud(
                        metodo, str(r.url), True, estado_http=r.status_code,
                        bytes=len(r.content), sha256=hashlib.sha256(r.content).digest(),
                        duracion_ms=_ms(inicio, self._reloj())))
                    if r.status_code not in REINTENTABLES:
                        self._fallos[host] = 0
                        r.raise_for_status()   # 4xx definitivo: no se reintenta
                        return r
                    ultimo_error = f"HTTP {r.status_code}"
                    retry_after = r.headers.get("Retry-After")
                if intento < self.max_intentos:
                    await self._dormir(self._backoff(intento, retry_after))

        self._fallos[host] += 1
        if self._fallos[host] >= self.umbral_circuito:
            self._abierto_hasta[host] = self._reloj() + self.enfriamiento_circuito
        raise ReintentosAgotados(f"{metodo} {url}: {ultimo_error}")

    async def _auditar(self, registro: RegistroSolicitud) -> None:
        if self._auditor is not None:
            await self._auditor(registro)


def _ms(inicio: float, fin: float) -> int:
    return int((fin - inicio) * 1000)


def _segundos_retry_after(valor: str) -> float | None:
    valor = valor.strip()
    if valor.isdigit():
        return float(valor)
    try:
        fecha = parsedate_to_datetime(valor)
    except (TypeError, ValueError):
        return None
    return max(0.0, (fecha - datetime.now(timezone.utc)).total_seconds())
