"""Logging, and the callback sink the console reads.

Two consumers want the same events: a terminal, where a human is watching a run
go past, and the Streamlit console, which renders the same lines inside the
page. `attach_sink` lets the second one subscribe without the pipeline knowing
anything about it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

LOGGER_NAME = "vmi"
Sink = Callable[[str, str], None]
"""Called with (level, message) for every record the package emits."""


class _SinkHandler(logging.Handler):
    def __init__(self, sink: Sink) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        # A broken UI must never take the pipeline down with it.
        with contextlib.suppress(Exception):
            self._sink(record.levelname, record.getMessage())


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str = "INFO", as_json: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            _JsonFormatter()
            if as_json
            else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")
        )
        logger.addHandler(handler)
    return logger


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


@contextmanager
def attach_sink(sink: Sink) -> Iterator[None]:
    """Mirror every log record to *sink* for the duration of the block."""
    handler = _SinkHandler(sink)
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
