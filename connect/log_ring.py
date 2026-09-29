"""In-process ring buffer of recent WARNING+ log records for diagnostics export.

``RingBufferHandler`` keeps the last 5000 records in a process-local deque.
Gunicorn workers do not share it, so a bundle only sees logs from the worker that
built it. Attach the handler from Django logging config; ``snapshot()`` copies the
deque under a lock for ``build_diagnostics_payload``.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

_RING: deque[dict[str, Any]] = deque(maxlen=5000)
_LOCK = threading.Lock()


class RingBufferHandler(logging.Handler):
    """Capture WARNING+ (and all ERROR) for diagnostics bundles."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        try:
            entry = {
                'ts': datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                'level': record.levelname,
                'logger': record.name,
                'message': self.format(record),
            }
            with _LOCK:
                _RING.append(entry)
        except Exception:
            pass


def recent_log_entries(*, limit: int = 200, min_level: str = 'WARNING') -> list[dict[str, Any]]:
    threshold = getattr(logging, min_level.upper(), logging.WARNING)
    with _LOCK:
        rows = list(_RING)
    out = []
    for row in reversed(rows):
        lvl = getattr(logging, str(row.get('level', 'INFO')), logging.INFO)
        if lvl >= threshold:
            out.append(row)
        if len(out) >= limit:
            break
    return list(reversed(out))
