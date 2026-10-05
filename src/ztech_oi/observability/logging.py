"""Structured JSON logging with secret redaction (spec #37).

Logs go to STDERR (stdout is reserved for the MCP stdio transport).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

_SECRET_KEYS = re.compile(r"(token|key|secret|password|authorization|bearer|cookie)", re.IGNORECASE)
_SECRET_VALUES = re.compile(r"(Bearer\s+[A-Za-z0-9._\-]+|access_token=[^&\s]+|api_key=[^&\s]+|key=[^&\s]+)")
_STD = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if _SECRET_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUES.sub("[REDACTED]", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for k, v in record.__dict__.items():
            if k not in _STD and not k.startswith("_"):
                payload[k] = redact(v)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def silence_third_party_url_logging() -> None:
    """httpx/httpcore log full request URLs (incl. query-string tokens) at INFO/DEBUG -> never allow that."""
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)


def configure_logging(level: str = "INFO") -> None:
    silence_third_party_url_logging()
    root = logging.getLogger("ztech_oi")
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
