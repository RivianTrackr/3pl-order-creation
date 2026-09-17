"""Capture a run's log output into the database for the admin Logs page."""

import logging
import re

from .db import Database

_REDACTIONS = (
    (re.compile(r"(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 ***"),
    (re.compile(r"(?i)(x-api-key|api[_-]?key|client_secret|access_token|password)(['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"),
     r"\1\2***"),
    (re.compile(r"SG\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "SG.***"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Masks secrets in everything that reaches a handler, including the console."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, ()
        return True


class DatabaseLogHandler(logging.Handler):
    """Writes log records for one run. Uses its own connection so log writes never
    commit, or get caught up in, the processor's transactions."""

    MAX_MESSAGE = 20000

    def __init__(self, db_path: str, run_id: int, level: int = logging.INFO):
        super().__init__(level)
        self.run_id = run_id
        self.db = Database(db_path)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self.db.add_log_line(self.run_id, record.levelname, record.name,
                                 redact(message)[: self.MAX_MESSAGE])
        except Exception:  # noqa: BLE001 - logging must never break a run
            self.handleError(record)

    def close(self) -> None:
        try:
            self.db.close()
        finally:
            super().close()
