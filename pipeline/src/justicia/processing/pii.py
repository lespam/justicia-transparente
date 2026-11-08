"""Enmascaramiento de datos personales (Bloque 1, sección 4.7).

El testado de los tribunales es incompleto: en los PDF reales de Nuevo León
hay nombres de particulares sin tapar. Esta capa agrega:

1. Patrones fijos: CURP, RFC, teléfonos, correos y domicilios.
2. Nombres de persona con el modelo spaCy es_core_news_md. El modelo no
   reconoce nombres en MAYÚSCULAS, así que esas secuencias se pasan a
   formato título antes de analizarlas (mismo largo, mismas posiciones).
3. Nombres pegados a un testado parcial: "José **********".
4. Propagación: un nombre detectado una vez se tapa en todo el documento,
   aunque el modelo no lo reconozca en otras apariciones.
5. Nombres de pila frecuentes seguidos de palabras con mayúscula inicial
   ("Antonio Díaz García", "CARLOS ALBERTO SALAS"), para lo que el modelo omite.

Criterio conservador: se enmascaran todas las personas salvo el juez de la
sentencia. Es preferible tapar de más a publicar un nombre de un particular.
Ninguna capa automática es perfecta: es una protección adicional, no una
garantía.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

PII_VERSION = "pii-3"

PATRONES = [
    ("CURP", re.compile(r"\b[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d\b")),
    ("RFC", re.compile(r"\b[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b")),
    ("CORREO", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # La palabra clave en cualquier caso, seguida de mayúscula o número:
    # "calle Hidalgo 123" sí; "privada de la libertad" no.
    ("DOMICILIO", re.compile(
        r"\b(?i:calle|avenida|av\.|colonia|col\.|privada|boulevard|blvd\.|fraccionamiento)"
        r"\s+(?=[A-ZÁÉÍÓÚÑ0-9])[^,.;:\n()]{2,60}")),
    # 10 dígitos, sin letras pegadas: excluye "registro digital 201061518"
    # y códigos internos como "SP02 52161667".
    ("TELÉFONO", re.compile(
        r"(?<![\w*/.-])(?:\+?52[\s-]?)?(?:\(\d{2,3}\)|\d{2,3})[\s-]?\d{3,4}[\s-]?\d{4}(?![\w*/-])")),
]

# Secuencias de 2 o más palabras en mayúsculas ("JUAN PÉREZ LÓPEZ").
_MAYUSCULAS = re.compile(r"\b[A-ZÁÉÍÓÚÑÜ]{2,}(?:[ \t]+[A-ZÁÉÍÓÚÑÜ]{1,}){1,}\b")
_TESTADO = re.compile(r"\*{5,}")

# Nombres de pila frecuentes en México (sin acentos, minúsculas).
NOMBRES_DE_PILA = frozenset("""
abel abraham adan adolfo adrian adriana agustin aide alan alberto alejandra
alejandro alexis alfonso alfredo alicia alma alonso alvaro amparo ana andrea
andres angel angela angelica antonio araceli armando arturo aurora beatriz
benito bernardo blanca brenda bruno carlos carmen carolina catalina cecilia
cesar christian claudia concepcion cristian cristina cristal daniel daniela
dario david diana diego dolores edgar edith eduardo efrain elena elias elizabeth
elsa emilio emmanuel enrique erick ernesto esmeralda esperanza esteban estela
eva fabiola federico felipe fernanda fernando francisca francisco gabriel
gabriela gerardo gilberto gloria graciela guadalupe guillermo gustavo hector
hilda hugo humberto ignacio irene irma irving isaac isabel ismael israel ivan
jaime javier jesus jessica joaquin jorge jose josefina juan juana julia julio
karina karla laura leonardo leticia lidia lilia liliana lorena lourdes lucia
luis luz manuel marco marcos margarita maria mariana mario marisol marlene
martha martin mauricio miguel mireya moises monica nancy natalia nicolas
noe norma octavio omar oscar osvaldo pablo patricia paulina pedro rafael
ramon raquel raul rebeca reyna ricardo roberto rocio rodolfo rodrigo rogelio
rosa rosario ruben salvador samuel sandra santiago sara saul sergio silvia
sofia susana teresa tomas ulises valeria vanessa veronica victor victoria
yolanda yesenia
""".split())

_ANTES_DE_TESTADO = re.compile(r"\b([A-ZÁÉÍÓÚÑ][A-Za-záéíóúñÁÉÍÓÚÑ]+)[ \t]?(?=\*{5,})")

# Nombre de pila + 1 a 3 palabras con mayúscula inicial.
_PALABRA = r"[A-ZÁÉÍÓÚÑ][A-Za-záéíóúñÁÉÍÓÚÑü]+"
_CANDIDATO = re.compile(rf"(?<!\bSan )(?<!\bSanta )(?<!\bSanto )\b{_PALABRA}(?:[ \t]+{_PALABRA}){{1,3}}\b")


@dataclass
class Resultado:
    texto: str
    conteos: Counter = field(default_factory=Counter)


@lru_cache(maxsize=1)
def _modelo():
    import spacy   # dependencia opcional: pip install -e '.[nlp]'
    return spacy.load("es_core_news_md", disable=["parser", "lemmatizer"])


def _clave(nombre: str) -> str:
    sin_acentos = unicodedata.normalize("NFD", nombre)
    sin_acentos = "".join(c for c in sin_acentos if unicodedata.category(c) != "Mn")
    return " ".join(sin_acentos.lower().split())


def _plegar(texto: str) -> str:
    """Minúsculas sin acentos, carácter por carácter (mismas posiciones)."""
    return "".join(unicodedata.normalize("NFD", c)[0] if c.strip() else " "
                   for c in texto.lower())


def _es_permitido(nombre: str, permitidos: set[str]) -> bool:
    clave = _clave(nombre)
    if len(clave.split()) < 2:
        return False
    return any(clave in p or p in clave for p in permitidos)


def _para_modelo(texto: str) -> str:
    """Pasa a formato título las secuencias en mayúsculas, sin cambiar largos."""
    def titulo(m: re.Match) -> str:
        nuevo = m.group(0).title()
        return nuevo if len(nuevo) == len(m.group(0)) else m.group(0)
    return _MAYUSCULAS.sub(titulo, texto)


def enmascarar(texto: str, conservar: list[str] | None = None,
               bloque: int = 100_000) -> Resultado:
    """Devuelve el texto con [PERSONA], [CURP], etc. y cuántos se taparon.

    `conservar`: nombres que no se tapan (el juez de la sentencia).
    """
    permitidos = {_clave(n) for n in (conservar or []) if len(_clave(n).split()) >= 2}
    tramos: list[tuple[int, int, str]] = []

    for etiqueta, patron in PATRONES:
        for m in patron.finditer(texto):
            digitos = re.sub(r"\D", "", m.group(0))
            if etiqueta == "TELÉFONO" and len(digitos.removeprefix("52") if len(digitos) == 12 else digitos) != 10:
                continue
            tramos.append((m.start(), m.end(), etiqueta))

    nlp = _modelo()
    preparado = _para_modelo(texto)
    nombres: set[str] = set()
    for inicio in range(0, len(preparado), bloque):
        doc = nlp(preparado[inicio:inicio + bloque])
        for ent in doc.ents:
            if ent.label_ == "PER" and not _es_permitido(ent.text, permitidos):
                tramos.append((inicio + ent.start_char, inicio + ent.end_char, "PERSONA"))
                if len(ent.text.split()) >= 2:
                    nombres.add(" ".join(_plegar(ent.text).split()))

    # "José **********": nombre de pila pegado a un testado parcial.
    for m in _ANTES_DE_TESTADO.finditer(texto):
        if _plegar(m.group(1)).strip() in NOMBRES_DE_PILA:
            tramos.append((m.start(1), m.end(1), "PERSONA"))

    for m in _CANDIDATO.finditer(texto):
        # "LICENCIADO JESUS PÉREZ": el nombre puede empezar a mitad de la secuencia.
        palabras = list(re.finditer(r"\S+", m.group(0)))
        for i, w in enumerate(palabras[:-1]):
            if _plegar(w.group(0)).strip() in NOMBRES_DE_PILA:
                inicio = m.start() + w.start()
                if not _es_permitido(texto[inicio:m.end()], permitidos):
                    tramos.append((inicio, m.end(), "PERSONA"))
                break

    if nombres:
        plegado = _plegar(texto)
        patron = re.compile(r"\b(?:" + "|".join(
            r"\s+".join(map(re.escape, n.split()))
            for n in sorted(nombres, key=len, reverse=True)) + r")\b")
        tramos += [(m.start(), m.end(), "PERSONA") for m in patron.finditer(plegado)]

    return _aplicar(texto, tramos)


def _aplicar(texto: str, tramos: list[tuple[int, int, str]]) -> Resultado:
    resultado = Resultado(texto="")
    partes, cursor = [], 0
    for inicio, fin, etiqueta in sorted(tramos, key=lambda t: (t[0], -t[1])):
        if inicio < cursor:      # tramo dentro de otro ya tapado
            continue
        partes += [texto[cursor:inicio], f"[{etiqueta}]"]
        resultado.conteos[etiqueta] += 1
        cursor = fin
    partes.append(texto[cursor:])
    resultado.texto = "".join(partes)
    return resultado
