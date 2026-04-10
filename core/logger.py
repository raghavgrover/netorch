"""
core/logger.py — Structured JSON logging for netorch.

Every log record is emitted as a single-line JSON object, which plays
well with journald, log shippers (Fluentd, Vector), and grep.

Usage:
    from core.logger import get_logger
    log = get_logger("executor")
    log.info("job_started", job_id="abc", device_count=10)
    log.error("device_failed", job_id="abc", host="10.0.0.1", error=str(e))
"""
from __future__ import annotations
import logging
import sys
import orjson
from datetime import datetime, timezone
from typing import Any


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, Any] = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname.lower(),
            "logger":  record.name,
            "msg":     record.getMessage(),
        }
        # Merge any extra fields passed via log.info(..., extra={...})
        for k, v in record.__dict__.items():
            if k not in (
                "args", "asctime", "created", "exc_info", "exc_text",
                "filename", "funcName", "id", "levelname", "levelno",
                "lineno", "module", "msecs", "message", "msg", "name",
                "pathname", "process", "processName", "relativeCreated",
                "stack_info", "thread", "threadName",
            ):
                base[k] = v
        if record.exc_info:
            base["exception"] = self.formatException(record.exc_info)
        return orjson.dumps(base).decode()


def get_logger(name: str) -> "_StructuredLogger":
    return _StructuredLogger(name)


class _StructuredLogger:
    """
    Thin wrapper that lets callers pass keyword args as structured fields:
        log.info("device_done", host="10.0.0.1", duration=4.2)
    """
    def __init__(self, name: str):
        self._log = logging.getLogger(f"netorch.{name}")
        if not self._log.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(_JsonFormatter())
            self._log.addHandler(handler)
            self._log.setLevel(logging.DEBUG)
            self._log.propagate = False

    def _emit(self, level: int, msg: str, **kwargs: Any) -> None:
        self._log.log(level, msg, extra=kwargs)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.ERROR, msg, **kwargs)
