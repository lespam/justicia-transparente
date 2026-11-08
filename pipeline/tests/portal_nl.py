"""Portal falso de Nuevo León para pruebas sin red.

Reproduce la estructura real de Penales.aspx (revisada el 2026-11-08):
tabla gdvPenal, botones de imagen por fila, paginador con __doPostBack,
script que abre Visor.aspx y la variable DEFAULT_URL. Como el portal real,
cada clic genera un uuid de archivo nuevo para el mismo PDF.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx

ROBOTS = """User-agent: *
Sitemap: http://www.exhortos.gob.mx/Portal2017/Sitemap.xml

User-agent: *
Disallow: /Prototipos/
Disallow: /consejojudicatura/
"""


@dataclass
class Sentencia:
    registro: str
    emision: str
    publicacion: str
    delito: str = "ROBO CON VIOLENCIA"
    juez: str = "JUEZ DE PRUEBA(Elabora)"
    pdf: bytes = b"%PDF-1.7 prueba"


def _fila(i: int, s: Sentencia) -> str:
    return (
        f'<tr><td class="d-none">{s.registro}</td><td>{s.emision}</td>'
        f"<td>{s.publicacion}</td><td>{s.delito}</td><td>{s.juez}</td>"
        f"<td>CONDENATORIO</td><td>CONDENA</td><td>"
        f'<input type="image" name="ctl00$ContentPlaceHolder1$gdvPenal$ctl{i + 2:02d}$ImgBtnSentencia"'
        f' id="ContentPlaceHolder1_gdvPenal_ImgBtnSentencia_{i}" src="x.svg" /></td></tr>'
    )


def pagina(sentencias: list[Sentencia], numero: int, total_paginas: int,
           visor: str | None = None) -> str:
    paginador = "".join(
        f"<td><a href=\"javascript:__doPostBack(&#39;ctl00$ContentPlaceHolder1$gdvPenal"
        f"&#39;,&#39;Page${n}&#39;)\">{n}</a></td>"
        for n in range(1, total_paginas + 1) if n != numero)
    script = ("<script>$('#ContentPlaceHolder1_framePDF').attr('src',"
              f"'{visor}');</script>" if visor else "")
    filas = "".join(_fila(i, s) for i, s in enumerate(sentencias))
    return f"""<html><body><form id="form1" method="post">
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="vs-p{numero}" />
<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="ev" />
<select name="ctl00$ContentPlaceHolder1$ddlTipoOrgano"><option selected="selected" value="0">Ver todos</option></select>
<input type="text" name="ctl00$ContentPlaceHolder1$txtSearch" value="" />
<table id="ContentPlaceHolder1_gdvPenal"><tr><th>Fecha</th></tr>{filas}
<tr><td colspan="8"><table><tr>{paginador}</tr></table></td></tr></table>
<iframe id="ContentPlaceHolder1_framePDF" src="about:blank"></iframe>
</form>{script}</body></html>"""


@dataclass
class PortalFalso:
    paginas: list[list[Sentencia]]
    solicitudes: list[tuple[str, str]] = field(default_factory=list)
    fallar_pdf: int = 0          # cuántas descargas de PDF responden 503

    def transporte(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._responder)

    def _por_registro(self, registro: str) -> Sentencia:
        return next(s for p in self.paginas for s in p if s.registro == registro)

    @property
    def clics(self) -> int:
        return sum(1 for m, r in self.solicitudes if r.endswith("Visor.aspx"))

    def _responder(self, req: httpx.Request) -> httpx.Response:
        ruta = req.url.path
        self.solicitudes.append((req.method, ruta))
        if ruta == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if ruta.endswith("Penales.aspx"):
            if req.method == "GET":
                return httpx.Response(200, text=pagina(self.paginas[0], 1, len(self.paginas)))
            datos = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            numero = int(datos["__VIEWSTATE"].removeprefix("vs-p"))
            if datos.get("__EVENTTARGET", "").endswith("gdvPenal"):
                numero = int(datos["__EVENTARGUMENT"].removeprefix("Page$"))
                return httpx.Response(200, text=pagina(
                    self.paginas[numero - 1], numero, len(self.paginas)))
            boton = next(k for k in datos if k.endswith("ImgBtnSentencia.x"))
            indice = int(boton.split("$ctl")[1][:2]) - 2
            s = self.paginas[numero - 1][indice]
            return httpx.Response(200, text=pagina(
                self.paginas[numero - 1], numero, len(self.paginas),
                visor=f"/SentenciasPublicas/PDFVisor/Visor.aspx?p=tok-{s.registro}-{uuid.uuid4()}"))
        if ruta.endswith("Visor.aspx"):
            archivo = req.url.params["p"].removeprefix("tok-")
            return httpx.Response(200, text=(
                "<script>var DEFAULT_URL = "
                f"'controller.aspx?file=~\\\\files\\\\{archivo}.pdf';</script>"))
        if ruta.endswith("controller.aspx"):
            if "file" in req.url.params:
                archivo = req.url.params["file"].split("\\")[-1].removesuffix(".pdf")
                return httpx.Response(302, headers={
                    "Location": f"/SentenciasPublicas/PDFVisor/controller.aspx?p=pdf-{archivo}"})
            if self.fallar_pdf > 0:
                self.fallar_pdf -= 1
                return httpx.Response(503)
            registro = req.url.params["p"].removeprefix("pdf-").split("-")[0]
            s = self._por_registro(registro)
            return httpx.Response(200, content=s.pdf,
                                  headers={"Content-Type": "application/pdf"})
        return httpx.Response(404)
