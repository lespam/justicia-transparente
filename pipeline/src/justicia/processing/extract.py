"""Extracción y normalización de texto.

Solo PDF por ahora: los 20 PDF reales de Nuevo León tienen capa de texto.
Los documentos sin texto suficiente se marcan para OCR; el OCR (OCRmyPDF +
Tesseract) se agrega con la imagen Docker del Bloque 4.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

import pypdfium2

EXTRACTOR_VERSION = "pdfium-1"

# Menos de esto por página indica un PDF escaneado (imagen sin texto).
MIN_CARACTERES_POR_PAGINA = 200


@dataclass(frozen=True)
class TextoExtraido:
    texto: str              # normalizado
    num_paginas: int
    necesita_ocr: bool

    @property
    def sha256(self) -> bytes:
        """Huella del texto normalizado (deduplicación nivel 3)."""
        return hashlib.sha256(self.texto.encode("utf-8")).digest()


def extraer_pdf(datos: bytes) -> TextoExtraido:
    pdf = pypdfium2.PdfDocument(datos)
    try:
        paginas = []
        for i in range(len(pdf)):
            pagina = pdf[i]
            paginas.append(pagina.get_textpage().get_text_range())
            pagina.close()
    finally:
        pdf.close()
    texto = normalizar("\n\n".join(paginas))
    sin_espacios = len(re.sub(r"\s", "", texto))
    necesita_ocr = sin_espacios < MIN_CARACTERES_POR_PAGINA * max(1, len(paginas))
    return TextoExtraido(texto, len(paginas), necesita_ocr)


_GUION_CORTE = re.compile(r"([a-záéíóúñü])-\n([a-záéíóúñü])")
_ESPACIOS = re.compile(r"[ \t ]+")
_SALTOS = re.compile(r"\n{3,}")


def normalizar(texto: str) -> str:
    """Texto estable: misma sentencia → mismos bytes, aunque cambie el PDF.

    1. Unicode NFC y sin caracteres de control (salvo saltos de línea).
    2. Une palabras cortadas con guion al final de línea.
    3. Un solo espacio entre palabras; sin espacios al final de línea.
    4. Máximo una línea en blanco seguida.
    """
    texto = unicodedata.normalize("NFC", texto)
    texto = texto.replace("\r\n", "\n").replace("\r", "\n").replace("­", "")
    texto = "".join(c for c in texto
                    if c == "\n" or unicodedata.category(c)[0] != "C")
    texto = _GUION_CORTE.sub(r"\1\2", texto)
    texto = _ESPACIOS.sub(" ", texto)
    texto = "\n".join(linea.strip() for linea in texto.split("\n"))
    return _SALTOS.sub("\n\n", texto).strip()
