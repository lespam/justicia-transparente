"""Línea de comandos del pipeline.

Ejemplos:
    justicia descubrir nl-penal --max-paginas 1
    justicia descargar nl-penal --limite 20
    justicia estado nl-penal
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import config
from .core.almacen import AlmacenLocal
from .core.db import Repositorio
from .core.logs import configurar_logs
from .flows.cosecha import FuenteNoDisponible, descargar, descubrir


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="justicia", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=config.DATABASE_URL, help="URL de PostgreSQL")
    sub = p.add_subparsers(dest="comando", required=True)

    d = sub.add_parser("descubrir", help="etapa 1: registrar items nuevos del listado")
    d.add_argument("fuente")
    d.add_argument("--max-paginas", type=int)
    d.add_argument("--max-nuevos", type=int)

    g = sub.add_parser("descargar", help="etapa 2: descargar documentos pendientes")
    g.add_argument("fuente")
    g.add_argument("--limite", type=int, default=50)
    g.add_argument("--almacen", default=str(config.ALMACEN_DIR))

    e = sub.add_parser("estado", help="documentos por estado")
    e.add_argument("fuente")
    return p.parse_args(argv)


async def _ejecutar(args: argparse.Namespace) -> dict:
    repo = await Repositorio.conectar(args.dsn)
    try:
        if args.comando == "descubrir":
            return await descubrir(repo, args.fuente, max_paginas=args.max_paginas,
                                   max_nuevos=args.max_nuevos)
        if args.comando == "descargar":
            return await descargar(repo, args.fuente, AlmacenLocal(args.almacen),
                                   limite=args.limite)
        filas = await repo.resumen(args.fuente)
        return {f["estado"]: f["total"] for f in filas}
    finally:
        await repo.cerrar()


def main(argv: list[str] | None = None) -> int:
    args = _argumentos(argv)
    configurar_logs()
    try:
        resultado = asyncio.run(_ejecutar(args))
    except FuenteNoDisponible as e:
        print(f"no se ejecutó: {e}", file=sys.stderr)
        return 2
    print(json.dumps(resultado, ensure_ascii=False))
    return 0 if resultado.get("estado", "exitosa") == "exitosa" else 1


if __name__ == "__main__":
    sys.exit(main())
