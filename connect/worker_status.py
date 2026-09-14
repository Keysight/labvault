"""Module-level worker/refresh thread status for diagnostics export."""
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
    with _lock:
        _device_refresh['last_run_at'] = time.time()
        _device_refresh['runs'] = int(_device_refresh.get('runs', 0)) + 1
        if error:
            _device_refresh['last_error'] = error[:500]


def record_keysight_refresh(*, error: str = '', leader: bool | None = None) -> None:
    with _lock:
        _keysight_refresh['last_run_at'] = time.time()
        _keysight_refresh['runs'] = int(_keysight_refresh.get('runs', 0)) + 1
        if error:
            _keysight_refresh['last_error'] = error[:500]
        if leader is not None:
            _keysight_refresh['leader'] = leader


def worker_status_payload() -> dict[str, Any]:
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
