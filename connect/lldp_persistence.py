"""
Persistent LLDP neighbor cache (24-hour retention).

Neighbors discovered on a successful scan are stored with timestamps.
If a later scan omits a neighbor, the previous record is kept for up to
LLDP_RETENTION_HOURS unless a new scan provides updated data for the same
local port (which replaces the record).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

LLDP_RETENTION_HOURS = 24
LLDP_RETENTION_SECONDS = LLDP_RETENTION_HOURS * 3600

_CACHE_PATH = Path(__file__).resolve().parent.parent / 'data' / 'lldp_persistent_cache.json'


def _now() -> float:
    return time.time()


def _port_key(neighbor: Dict[str, Any]) -> str:
    return (neighbor.get('local_port') or neighbor.get('port') or '').strip().lower()


def _normalize_neighbor(n: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'local_port': (n.get('local_port') or n.get('port') or '').strip(),
        'remote_device': (n.get('remote_device') or n.get('system_name') or '').strip(),
        'remote_port': (n.get('remote_port') or n.get('port_id') or '').strip(),
        'mgmt_ip': (n.get('mgmt_ip') or n.get('management_address') or '').strip(),
        'chassis_id': (n.get('chassis_id') or '').strip(),
        'up': n.get('up', True),
    }


def _load_store() -> Dict[str, Any]:
    if not _CACHE_PATH.is_file():
        return {'version': 1, 'entities': {}}
    try:
        data = json.loads(_CACHE_PATH.read_text())
        if isinstance(data, dict) and 'entities' in data:
            return data
    except Exception as exc:
        logger.warning('lldp_persistence: read failed: %s', exc)
    return {'version': 1, 'entities': {}}


def _save_store(store: Dict[str, Any]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    store['updated_at'] = _now()
    _CACHE_PATH.write_text(json.dumps(store, indent=2))


def _entity_key(kind: str, entity_id: int | str) -> str:
    return f'{kind}:{entity_id}'


def merge_neighbors(
    existing: List[Dict[str, Any]],
    fresh: List[Dict[str, Any]],
    *,
    fresh_scan_ok: bool,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Merge fresh LLDP neighbors into existing list.

    - Fresh entries (by local_port) replace or add with updated_at=now.
    - Missing from fresh scan: keep if last_seen within retention window.
    - Empty fresh on failed scan (fresh_scan_ok=False): return pruned existing only.
    """
    now = _now()
    cutoff = now - LLDP_RETENTION_SECONDS
    by_port: Dict[str, Dict[str, Any]] = {}

    for raw in existing or []:
        n = _normalize_neighbor(raw)
        if not n['local_port']:
            continue
        ts = float(raw.get('_updated_at') or raw.get('last_seen_ts') or 0)
        if ts >= cutoff:
            n['_updated_at'] = ts
            by_port[_port_key(n)] = n

    changed = False
    if fresh_scan_ok:
        fresh_ports = set()
        for raw in fresh or []:
            n = _normalize_neighbor(raw)
            if not n['local_port']:
                continue
            pk = _port_key(n)
            fresh_ports.add(pk)
            prev = by_port.get(pk)
            n['_updated_at'] = now
            if not prev or prev.get('remote_device') != n['remote_device'] or prev.get('remote_port') != n['remote_port']:
                changed = True
            by_port[pk] = n
        # Ports not in fresh scan: keep if still within retention (already in by_port)
        for pk, n in list(by_port.items()):
            if pk not in fresh_ports and n.get('_updated_at', 0) < cutoff:
                del by_port[pk]
                changed = True

    merged = sorted(by_port.values(), key=lambda x: x.get('local_port', ''))
    return merged, changed


def persist_entity_neighbors(
    kind: str,
    entity_id: int | str,
    fresh_neighbors: List[Dict[str, Any]],
    *,
    fresh_scan_ok: bool,
) -> List[Dict[str, Any]]:
    """Load, merge, save, and return persisted neighbors for one entity."""
    store = _load_store()
    key = _entity_key(kind, entity_id)
    entry = store['entities'].get(key) or {}
    existing = entry.get('neighbors') or []
    merged, _changed = merge_neighbors(existing, fresh_neighbors, fresh_scan_ok=fresh_scan_ok)
    store['entities'][key] = {
        'kind': kind,
        'id': str(entity_id),
        'neighbors': merged,
        'last_scan_ok': fresh_scan_ok,
        'last_scan_at': _now() if fresh_scan_ok else entry.get('last_scan_at'),
    }
    _save_store(store)
    return merged


def get_entity_neighbors(kind: str, entity_id: int | str) -> List[Dict[str, Any]]:
    """Return persisted neighbors still within retention (pruned)."""
    store = _load_store()
    key = _entity_key(kind, entity_id)
    entry = store['entities'].get(key) or {}
    merged, _ = merge_neighbors(entry.get('neighbors') or [], [], fresh_scan_ok=False)
    return merged


def persist_switch_lldp_cache(devices_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge Arista switch LLDP into persistent store (used by refresh_lldp).
    devices_payload: {ip: {neighbors: [...], ...}, ...}
    """
    store = _load_store()
    for ip, payload in (devices_payload or {}).items():
        if not isinstance(payload, dict):
            continue
        neighbors = payload.get('neighbors') or []
        key = _entity_key('switch', ip)
        entry = store['entities'].get(key) or {}
        merged, _ = merge_neighbors(
            entry.get('neighbors') or [],
            neighbors,
            fresh_scan_ok=bool(neighbors) or payload.get('from_file_fallback'),
        )
        store['entities'][key] = {
            'kind': 'switch',
            'id': ip,
            'neighbors': merged,
            'last_scan_ok': bool(neighbors),
            'last_scan_at': _now(),
            'active_os': payload.get('active_os', ''),
        }
    _save_store(store)
    return store


def load_switch_lldp_by_ip() -> Dict[str, List[Dict[str, Any]]]:
    """Return {ip: [neighbors]} from persistent cache for switches."""
    store = _load_store()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key, entry in (store.get('entities') or {}).items():
        if not key.startswith('switch:'):
            continue
        ip = entry.get('id') or key.split(':', 1)[-1]
        merged, _ = merge_neighbors(entry.get('neighbors') or [], [], fresh_scan_ok=False)
        if merged:
            out[ip] = merged
    return out
