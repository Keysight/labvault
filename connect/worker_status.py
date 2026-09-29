"""Module-level worker/refresh thread status for diagnostics export.

State is per-process memory. It is updated only by the in-process refresh
threads (``views._refresh_all_devices`` and ``keysight_views._ks_refresh_all``).
The Keysight thread never starts under gunicorn or with
``LABVAULT_DISABLE_INPROCESS_REFRESH=1``, and the ``run_labvault_refresh``
worker does not call :func:`record_keysight_refresh`, so on a normal install
the web process reports ``last_run_at=None`` / ``stale=True`` for the Keysight
row even when the chassis cache is fresh. A row is stale after ``3 ×`` its interval
(``REFRESH_INTERVAL`` default 20 s, ``KS_REFRESH_INTERVAL`` default 120 s).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

_lock = threading.Lock()
_device_refresh: dict[str, Any] = {
    'last_run_at': None,
    'last_error': '',
    'runs': 0,
}
_keysight_refresh: dict[str, Any] = {
    'last_run_at': None,
    'last_error': '',
    'runs': 0,
    'leader': False,
}


def record_device_refresh(*, error: str = '') -> None:
    """Stamp one device-refresh loop iteration (error kept, truncated to 500 chars)."""
    with _lock:
        _device_refresh['last_run_at'] = time.time()
        _device_refresh['runs'] = int(_device_refresh.get('runs', 0)) + 1
        if error:
            _device_refresh['last_error'] = error[:500]


def record_keysight_refresh(*, error: str = '', leader: bool | None = None) -> None:
    """Stamp one Keysight refresh iteration; ``leader`` records the flock outcome."""
    with _lock:
        _keysight_refresh['last_run_at'] = time.time()
        _keysight_refresh['runs'] = int(_keysight_refresh.get('runs', 0)) + 1
        if error:
            _keysight_refresh['last_error'] = error[:500]
        if leader is not None:
            _keysight_refresh['leader'] = leader


def worker_status_payload() -> dict[str, Any]:
    """Snapshot for ``connect.diagnostics``: both rows with ``age_s`` / ``stale`` plus intervals."""
    now = time.time()
    with _lock:
        dev = dict(_device_refresh)
        ks = dict(_keysight_refresh)
    interval = int(os.environ.get('REFRESH_INTERVAL', '20') or 20)
    ks_interval = int(os.environ.get('KS_REFRESH_INTERVAL', '120') or 120)
    for row, iv in ((dev, interval), (ks, ks_interval)):
        ts = row.get('last_run_at')
        if ts:
            row['age_s'] = round(now - float(ts), 1)
            row['stale'] = (now - float(ts)) > iv * 3
        else:
            row['age_s'] = None
            row['stale'] = True
    return {
        'device_refresh_thread': dev,
        'keysight_refresh_thread': ks,
        'device_refresh_interval_s': interval,
        'keysight_refresh_interval_s': ks_interval,
    }
