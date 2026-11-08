"""Adaptador del Poder Judicial de Nuevo León, materia penal.

Cómo funciona el portal (revisado el 2026-11-08):
1. El listado está en Modulos/Penales.aspx. Es un formulario ASP.NET
   (WebForms) con 20 filas por página, de la sentencia más reciente a la
   más antigua según la fecha de emisión.
2. Cada fila tiene un botón de imagen. Al hacer clic (POST con __VIEWSTATE)
   el portal responde la misma página con un script que abre
   PDFVisor/Visor.aspx?p=<token>.
3. Visor.aspx contiene `var DEFAULT_URL = 'controller.aspx?file=~\\files\\<id>.pdf'`.
4. controller.aspx redirige a una URL temporal que entrega el PDF.

Llave estable: el número de registro (columna oculta del listado).
- El uuid del archivo es TEMPORAL: cada clic genera uno nuevo, pero el PDF es
  el mismo (mismo SHA-256). No sirve como llave.
- Un registro puede aparecer en varias filas (por ejemplo, un delito por
  fila). Las filas de una misma página se agrupan en un solo documento.
- Como el registro se ve antes de hacer clic, las filas ya conocidas se saltan
  sin ninguna solicitud extra.
- Las URLs temporales de 2021 (sentencias_with_url.csv) ya no funcionan; una
  de una hora sí. La descarga debe correr poco después del descubrimiento.
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import date

from bs4 import BeautifulSoup

from ..core.fechas import parse_fecha
from ..processing.metadata import Metadatos, esta_testado, sentido_desde_texto
from ..core.http import ClienteCortes
from .base import DocumentoInvalido, ListingItem, RawDocument

BASE = "https://www.pjenl.gob.mx"
URL_LISTADO = f"{BASE}/SentenciasPublicas/Modulos/Penales.aspx"
URL_VISOR_DIR = f"{BASE}/SentenciasPublicas/PDFVisor/"
GRID = "ctl00$ContentPlaceHolder1$gdvPenal"

_RE_VISOR = re.compile(r"framePDF'\)\.attr\('src',\s*'([^']*Visor\.aspx[^']*)'")
_RE_DEFAULT_URL = re.compile(r"var DEFAULT_URL = '(.*?)';")
_RE_ID_ARCHIVO = re.compile(r"files\\+(\d+-[0-9a-fA-F-]{36})\.pdf")

# Encabezado de los PDF (revisado en 20 sentencias reales, 2026-11-08):
#   *CO000036718320*  /  SENTENCIAS  /  SE DICTA SENTENCIA DE PROCEDIMIENTO ABREVIADO
#   PODER JUDICIAL DEL ESTADO DE NUEVO LEÓN  /  En Monterrey, Nuevo León, a …
_RE_CODIGO = re.compile(r"\bCO\d{12}\b")
_RE_TIPO = re.compile(r"^SE DICTA (.+)$", re.MULTILINE)
_RE_MUNICIPIO = re.compile(
    r"(?:^|\n)\.?\s*(?:En\s+)?(?:la\s+ciudad\s+de\s+)?([A-ZÁÉÍÓÚÑ][\wáéíóúñ ]+?),\s*Nuevo\s+Le[oó]n")
_RE_CARPETA = re.compile(r"carpeta judicial(?:\s+n[uú]mero)?\s+([\d*]+/\d{4})", re.IGNORECASE)
_RE_ELABORA = re.compile(r"\s*\([^)]*\)\s*$")


@dataclass(frozen=True)
class FilaListado:
    registro: str             # número visible (no único)
    boton: str                # name del botón de imagen de la fila
    fecha_emision: str
    fecha_publicacion: str
    delitos: str
    juez: str
    sentido_fallo: str
    descripcion: str


# -- Funciones puras de lectura de HTML (se prueban sin red) ------------------

def estado_formulario(pagina: str) -> dict[str, str]:
    """Campos que el navegador enviaría en un postback (sin botones)."""
    soup = BeautifulSoup(pagina, "html.parser")
    form = soup.find("form")
    campos: dict[str, str] = {}
    for entrada in form.find_all("input"):
        nombre = entrada.get("name")
        if nombre and entrada.get("type", "text") in ("hidden", "text"):
            campos[nombre] = entrada.get("value", "")
    for lista in form.find_all("select"):
        opcion = lista.find("option", selected=True) or lista.find("option")
        if lista.get("name"):
            campos[lista["name"]] = opcion.get("value", "") if opcion else ""
    campos["__EVENTTARGET"] = ""
    campos["__EVENTARGUMENT"] = ""
    return campos


def leer_filas(pagina: str) -> list[FilaListado]:
    soup = BeautifulSoup(pagina, "html.parser")
    tabla = soup.find(id="ContentPlaceHolder1_gdvPenal")
    if tabla is None:
        return []
    filas = []
    for tr in tabla.find_all("tr", recursive=False) or tabla.select("tr"):
        boton = tr.find("input", attrs={"type": "image"})
        celdas = tr.find_all("td", recursive=False)
        if boton is None or len(celdas) < 8:
            continue   # encabezado o paginador
        texto = [c.get_text(" ", strip=True) for c in celdas]
        filas.append(FilaListado(
            registro=texto[0], boton=boton["name"], fecha_emision=texto[1],
            fecha_publicacion=texto[2], delitos=texto[3], juez=texto[4],
            sentido_fallo=texto[5], descripcion=texto[6],
        ))
    return filas


def hay_pagina(pagina: str, numero: int) -> bool:
    """True si el paginador ofrece ir a la página `numero`."""
    return f"Page${numero}" in html.unescape(pagina)


def url_visor(pagina: str) -> str | None:
    m = _RE_VISOR.search(pagina)
    return BASE + html.unescape(m.group(1)) if m else None


def url_archivo(visor: str) -> str | None:
    """URL directa del PDF a partir del HTML de Visor.aspx."""
    m = _RE_DEFAULT_URL.search(visor)
    if not m:
        return None
    ruta = m.group(1).replace("\\\\", "\\")   # el JS escapa las diagonales
    return URL_VISOR_DIR + ruta


def id_archivo(url: str) -> str | None:
    m = _RE_ID_ARCHIVO.search(url)
    return m.group(1) if m else None


def _item(filas: list[FilaListado], url: str) -> ListingItem:
    """Un documento por registro, con todas sus filas del listado."""
    archivo = id_archivo(url)
    if archivo is None:
        raise DocumentoInvalido(f"URL sin id de archivo: {url}")
    datos_filas = []
    for fila in filas:
        datos = asdict(fila)
        del datos["boton"], datos["registro"]
        emision = parse_fecha(fila.fecha_emision)
        datos["fecha_emision_iso"] = emision.isoformat() if emision else None
        datos_filas.append(datos)
    publicadas = [d for f in filas if (d := parse_fecha(f.fecha_publicacion))]
    return ListingItem(
        source_key=filas[0].registro,
        url=url,
        fecha_publicacion=max(publicadas, default=None),
        datos={"registro": filas[0].registro, "archivo_temporal": archivo,
               "filas": datos_filas},
    )


def agrupar(filas: list[FilaListado]) -> list[list[FilaListado]]:
    """Agrupa las filas por registro, conservando el orden del listado."""
    grupos: dict[str, list[FilaListado]] = {}
    for fila in filas:
        grupos.setdefault(fila.registro, []).append(fila)
    return list(grupos.values())


# -- Adaptador ----------------------------------------------------------------

class NuevoLeonPenal:
    fuente = "nl-penal"
    metadatos_version = "nl-penal-1"

    def __init__(self, cliente: ClienteCortes):
        self.cliente = cliente

    async def discover(
        self,
        desde: date | None,
        *,
        es_conocido: Callable[[str], Awaitable[bool]] | None = None,
        max_paginas: int | None = None,
    ) -> AsyncIterator[ListingItem]:
        """Recorre el listado de lo más reciente a lo más antiguo.

        - Ignora filas con fecha de publicación anterior a `desde`.
        - Salta sin hacer clic los registros que `es_conocido` reconoce.
        - Se detiene cuando todas las filas de una página fueron emitidas
          antes de `desde`: las páginas siguientes son aún más antiguas.
        """
        pagina = (await self.cliente.get(URL_LISTADO)).text
        numero = 1
        while True:
            filas = leer_filas(pagina)
            if not filas:
                return
            formulario = estado_formulario(pagina)
            for grupo in agrupar(filas):
                publicadas = [parse_fecha(f.fecha_publicacion) for f in grupo]
                if desde and all(p and p < desde for p in publicadas):
                    continue
                if es_conocido and await es_conocido(grupo[0].registro):
                    continue
                url, pagina_clic = await self._resolver(grupo[0], formulario)
                formulario = estado_formulario(pagina_clic)   # nuevo __VIEWSTATE
                yield _item(grupo, url)

            emisiones = [parse_fecha(f.fecha_emision) for f in filas]
            if desde and all(e and e < desde for e in emisiones):
                return
            if max_paginas and numero >= max_paginas:
                return
            if not hay_pagina(pagina, numero + 1):
                return
            numero += 1
            formulario.update(__EVENTTARGET=GRID, __EVENTARGUMENT=f"Page${numero}")
            pagina = (await self.cliente.post(
                URL_LISTADO, data=formulario, headers={"Referer": URL_LISTADO})).text

    async def _resolver(self, fila: FilaListado, formulario: dict[str, str]
                        ) -> tuple[str, str]:
        """Clic en la fila → Visor.aspx → URL directa del PDF."""
        datos = dict(formulario, **{f"{fila.boton}.x": "8", f"{fila.boton}.y": "8"})
        pagina_clic = (await self.cliente.post(
            URL_LISTADO, data=datos, headers={"Referer": URL_LISTADO})).text
        visor = url_visor(pagina_clic)
        if visor is None:
            raise DocumentoInvalido(f"sin Visor.aspx para el registro {fila.registro}")
        url = url_archivo((await self.cliente.get(visor)).text)
        if url is None:
            raise DocumentoInvalido(f"sin DEFAULT_URL para el registro {fila.registro}")
        return url, pagina_clic

    def parse_metadata(self, texto: str, item: ListingItem) -> Metadatos:
        """Metadatos de la fila del listado, completados con el encabezado del PDF.

        El listado es la fuente principal (delitos, juez, sentido, fechas).
        El texto aporta el tipo de resolución, el municipio, el código del
        documento y, si el listado viene vacío, el sentido del fallo.
        """
        filas = item.datos.get("filas", [])
        primera = filas[0] if filas else {}
        delitos: list[str] = []
        for fila in filas:
            for delito in (fila.get("delitos") or "").split(","):
                delito = " ".join(delito.split()).upper()
                if delito and delito not in delitos:
                    delitos.append(delito)

        inicio = texto[:3000]
        extra: dict[str, str] = {}
        if m := _RE_CODIGO.search(inicio):
            extra["codigo_documento"] = m.group(0)
        if m := _RE_TIPO.search(inicio):
            extra["tipo_resolucion"] = m.group(1).strip()
        if "ABREVIADO" in extra.get("tipo_resolucion", "") or "procedimiento abreviado" in inicio.lower():
            extra["procedimiento"] = "abreviado"
        elif "juicio oral" in inicio.lower():
            extra["procedimiento"] = "juicio oral"
        if m := _RE_MUNICIPIO.search(inicio):
            extra["municipio"] = m.group(1).strip()
        expediente = None
        if (m := _RE_CARPETA.search(inicio)) and not esta_testado(m.group(1)):
            expediente = m.group(1)

        emision = primera.get("fecha_emision_iso")
        juez = _RE_ELABORA.sub("", primera.get("juez") or "").strip() or None
        sentido = (primera.get("sentido_fallo") or "").strip() or None
        if sentido is None and (sentido := sentido_desde_texto(texto)):
            extra["sentido_origen"] = "texto"
        return Metadatos(
            materia="penal",
            fecha_emision=date.fromisoformat(emision) if emision else None,
            fecha_publicacion=item.fecha_publicacion,
            expediente=expediente,
            juez=juez,
            sentido_fallo=sentido,
            delitos=delitos,
            descripcion=(primera.get("descripcion") or "").strip() or None,
            extra=extra,
        )

    async def fetch(self, item: ListingItem) -> RawDocument:
        r = await self.cliente.get(item.url)
        if not r.content.startswith(b"%PDF"):
            raise DocumentoInvalido(
                f"no es PDF ({r.headers.get('content-type')}, {len(r.content)} bytes)")
        return RawDocument(r.content, "application/pdf", str(r.url))
