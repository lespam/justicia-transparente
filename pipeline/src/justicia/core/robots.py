"""Lectura de robots.txt según RFC 9309.

No se usa urllib.robotparser porque solo toma el primer grupo "User-agent: *".
El robots.txt de pjenl.gob.mx tiene dos grupos "*": el primero solo trae un
Sitemap y el segundo trae las reglas Disallow. La RFC indica combinarlos.
"""

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Regla:
    permitir: bool
    patron: str
    regex: re.Pattern = field(compare=False)

    @classmethod
    def crear(cls, permitir: bool, patron: str) -> "Regla":
        ancla = patron.endswith("$")
        cuerpo = patron[:-1] if ancla else patron
        regex = ".*".join(re.escape(parte) for parte in cuerpo.split("*"))
        return cls(permitir, patron, re.compile(regex + ("$" if ancla else "")))


class ReglasRobots:
    def __init__(self, reglas: list[Regla]):
        self.reglas = reglas

    @classmethod
    def permitir_todo(cls) -> "ReglasRobots":
        return cls([])

    @classmethod
    def bloquear_todo(cls) -> "ReglasRobots":
        return cls([Regla.crear(False, "/")])

    @classmethod
    def desde_texto(cls, texto: str, agente: str) -> "ReglasRobots":
        """Reglas que aplican al agente. `agente` es el User-Agent completo."""
        token = agente.split("/")[0].strip().lower()
        grupos: list[tuple[set[str], list[Regla]]] = []
        actual: tuple[set[str], list[Regla]] | None = None
        en_reglas = False

        for linea in texto.splitlines():
            linea = linea.split("#", 1)[0].strip()
            if ":" not in linea:
                continue
            campo, valor = (p.strip() for p in linea.split(":", 1))
            campo = campo.lower()
            if campo == "user-agent":
                if actual is None or en_reglas:
                    actual = (set(), [])
                    grupos.append(actual)
                    en_reglas = False
                actual[0].add(valor.lower())
            elif campo in ("allow", "disallow") and actual is not None:
                en_reglas = True
                if valor:  # "Disallow:" vacío no bloquea nada
                    actual[1].append(Regla.crear(campo == "allow", valor))

        propios = [g for g in grupos if token in g[0]]
        aplicables = propios or [g for g in grupos if "*" in g[0]]
        return cls([r for _, reglas in aplicables for r in reglas])

    def permite(self, ruta: str) -> bool:
        """Gana la regla más larga; en empate gana Allow."""
        if ruta == "/robots.txt":
            return True
        coincidencias = [r for r in self.reglas if r.regex.match(ruta)]
        if not coincidencias:
            return True
        return max(coincidencias, key=lambda r: (len(r.patron), r.permitir)).permitir
