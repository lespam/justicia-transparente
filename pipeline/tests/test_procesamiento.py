"""Etapa 3: extracción, metadatos y enmascaramiento (sin red ni base de datos)."""

from datetime import date

import pytest

from justicia.adapters.base import ListingItem
from justicia.adapters.nuevo_leon import NuevoLeonPenal
from justicia.processing import pii
from justicia.processing.extract import extraer_pdf, normalizar
from justicia.processing.metadata import problemas, sentido_desde_texto

from .pdf_simple import crear_pdf, sentencia_nl


def modelo_disponible() -> bool:
    try:
        pii._modelo()
    except (ImportError, OSError):
        return False
    return True


requiere_spacy = pytest.mark.skipif(not modelo_disponible(),
                                    reason="instala el extra: pip install -e '.[nlp]'")


# -- Extracción y normalización -----------------------------------------------

def test_normalizar():
    sucio = "Se  dicta senten-\ncia   \r\n\n\n\nfinal­.  "
    assert normalizar(sucio) == "Se dicta sentencia\n\nfinal."


def test_extraer_pdf():
    extraido = extraer_pdf(sentencia_nl())
    assert extraido.num_paginas == 2
    assert not extraido.necesita_ocr
    assert "SE DICTA SENTENCIA DE PROCEDIMIENTO ABREVIADO" in extraido.texto
    assert "San Nicolás de los Garza" in extraido.texto   # acentos intactos


def test_pdf_sin_texto_necesita_ocr():
    assert extraer_pdf(crear_pdf([["1"], ["2"]])).necesita_ocr


def test_mismo_texto_misma_huella():
    a, b = extraer_pdf(sentencia_nl()), extraer_pdf(sentencia_nl())
    assert a.sha256 == b.sha256
    assert extraer_pdf(sentencia_nl(codigo="CO000099999999")).sha256 != a.sha256


# -- Metadatos ---------------------------------------------------------------------

def item(filas: list[dict]) -> ListingItem:
    return ListingItem("489000", "u", date(2026, 11, 7), {"registro": "489000", "filas": filas})


FILA = {"fecha_emision_iso": "2026-11-06", "delitos": "ROBO CALIFICADO, ROBO",
        "juez": "ANA LAURA TREVIÑO GARZA(Elabora)", "sentido_fallo": "CONDENATORIO",
        "descripcion": "CONDENA ABREVIADO"}


def test_metadatos_nuevo_leon():
    texto = extraer_pdf(sentencia_nl()).texto
    otra = dict(FILA, delitos="ROBO, LESIONES")
    m = NuevoLeonPenal(None).parse_metadata(texto, item([FILA, otra]))
    assert m.delitos == ["ROBO CALIFICADO", "ROBO", "LESIONES"]
    assert m.juez == "ANA LAURA TREVIÑO GARZA"
    assert m.fecha_emision == date(2026, 11, 6)
    assert m.expediente is None                  # la carpeta viene testada
    assert m.extra == {
        "codigo_documento": "CO000012345678",
        "tipo_resolucion": "SENTENCIA DE PROCEDIMIENTO ABREVIADO",
        "procedimiento": "abreviado",
        "municipio": "San Nicolás de los Garza",
    }
    assert problemas(m, texto) == []


def test_sentido_desde_texto_si_el_listado_no_lo_trae():
    texto = extraer_pdf(sentencia_nl(sentido="absuelve")).texto
    m = NuevoLeonPenal(None).parse_metadata(texto, item([dict(FILA, sentido_fallo="")]))
    assert m.sentido_fallo == "ABSOLUTORIO"
    assert m.extra["sentido_origen"] == "texto"
    assert sentido_desde_texto("sin palabras clave") is None


def test_sin_delitos_va_a_revision():
    texto = extraer_pdf(sentencia_nl()).texto
    m = NuevoLeonPenal(None).parse_metadata(texto, item([dict(FILA, delitos="")]))
    assert problemas(m, texto) == ["sin delitos"]


# -- Enmascaramiento -----------------------------------------------------------------

@pytest.mark.parametrize("texto, esperado", [
    ("llamó al 81 1234 5678 ayer", "llamó al [TELÉFONO] ayer"),
    ("registro digital 201061518", "registro digital 201061518"),
    ("código SP02 52161667", "código SP02 52161667"),
    ("vive en calle Hidalgo 123, Monterrey", "vive en [DOMICILIO], Monterrey"),
    ("persona privada de la libertad", "persona privada de la libertad"),
    ("CURP GOLR800101HNLMPS09 vigente", "CURP [CURP] vigente"),
    ("correo a.b@ejemplo.mx hoy", "correo [CORREO] hoy"),
])
def test_patrones_fijos(texto, esperado):
    tramos = []
    for etiqueta, patron in pii.PATRONES:
        tramos += [(m.start(), m.end(), etiqueta) for m in patron.finditer(texto)
                   if etiqueta != "TELÉFONO" or len(__import__("re").sub(r"\D", "", m.group(0))) == 10]
    assert pii._aplicar(texto, tramos).texto == esperado


@requiere_spacy
def test_enmascara_nombres_y_conserva_al_juez():
    texto = (
        "El testigo Pedro Gómez Ruiz declaró. Después MARIO ALBERTO SOTO LUNA aceptó los hechos. "
        "Más tarde Mario Alberto Soto Luna se retiró. La víctima José **********y otro. "
        "Resolvió la Jueza Ana Laura Treviño Garza. Aplica el Pacto de San José de Costa Rica."
    )
    r = pii.enmascarar(texto, conservar=["ANA LAURA TREVIÑO GARZA"])
    for visible in ("Pedro", "MARIO", "Mario Alberto", "José **"):
        assert visible not in r.texto
    assert "Ana Laura Treviño Garza" in r.texto
    assert "San José de Costa Rica" in r.texto
    assert r.conteos["PERSONA"] >= 4
