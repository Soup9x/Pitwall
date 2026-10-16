"""Structured (JSON lines) logging to stdout, with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "color_message",  # uvicorn's ANSI-coloured duplicate of msg
}


_SECRETS: set[str] = set()
_SORTED: list[str] = []
# Calendar and kiosk links carry their secret as ?token=...; access logs must never show it.
_URL_TOKEN = re.compile(r"(\btoken=)[^&\s\"']+")


def register_secrets(values: list[str]) -> None:
    """Add values that must never appear in logs (e.g. a notification URL added in the UI)."""
    global _SORTED
    _SECRETS.update(v for v in values if v and len(v) >= 4)
    _SORTED = sorted(_SECRETS, key=len, reverse=True)


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        register_secrets(secrets)

    @property
    def secrets(self) -> list[str]:
        return _SORTED

    def _scrub(self, text: str) -> str:
        for s in self.secrets:
            if s in text:
                text = text.replace(s, "***")
        return _URL_TOKEN.sub(r"\1***", text) if "token=" in text else text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(record.getMessage())
        record.args = None
        for key, value in list(record.__dict__.items()):
            if key not in _STD_ATTRS and isinstance(value, str):
                setattr(record, key, self._scrub(value))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter(secrets or []))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
