"""Logs estructurados en JSON (Bloque 1: run_id, fuente y sentencia en cada línea)."""

import logging

import structlog


def configurar_logs(nivel: int = logging.INFO) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(nivel),
    )
