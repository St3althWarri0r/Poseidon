"""Structured logging setup (structlog + stdlib).

Two sinks:
  * console — human-readable, colorized when attached to a TTY
  * file    — JSON lines under the data directory, rotated by size

Secrets never reach the log: a processor redacts values for keys that look
like credentials before rendering.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

_SENSITIVE_MARKERS = ("key", "secret", "token", "password", "passphrase", "authorization")


def _redact(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for k in list(event_dict):
        lk = k.lower()
        if any(marker in lk for marker in _SENSITIVE_MARKERS):
            event_dict[k] = "***redacted***"
    return event_dict


def configure_logging(log_dir: Path, level: str = "INFO", *, json_console: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _redact,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    console_renderer: Any
    if json_console or not sys.stderr.isatty():
        console_renderer = structlog.processors.JSONRenderer()
    else:
        console_renderer = structlog.dev.ConsoleRenderer(colors=True)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(processor=console_renderer, foreign_pre_chain=shared_processors)
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "poseidon.jsonl", maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(), foreign_pre_chain=shared_processors
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    root.setLevel(level.upper())

    # Quiet noisy third-party loggers; our own logs carry the signal.
    for noisy in ("httpx", "httpcore", "uvicorn.access", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
