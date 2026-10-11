"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed via `extra={...}` becomes a top-level field.
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


class RedactingFilter(logging.Filter):
    """Replaces any registered secret value that slips into a formatted record."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []

    def add(self, *secrets: str) -> None:
        self._secrets.extend(s for s in secrets if s and len(s) >= 4)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        changed = False
        for s in self._secrets:
            if s in msg:
                msg = msg.replace(s, "***")
                changed = True
        if changed:
            record.msg, record.args = msg, None
        for k, v in list(record.__dict__.items()):
            if k not in _STD_ATTRS and isinstance(v, str):
                for s in self._secrets:
                    if s in v:
                        v = v.replace(s, "***")
                record.__dict__[k] = v
        return True


redactor = RedactingFilter()


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(redactor)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # websockets logs every frame at DEBUG; keep it quiet.
    logging.getLogger("websockets").setLevel(logging.WARNING)
