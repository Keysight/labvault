"""
Persistent + in-memory cache for Lab Topology fabric API payloads.

Layering (fastest first):
  1. Django cache (process-local / shared if Redis configured)
  2. LabTopologyFabricSnapshot (Postgres JSON — survives restarts, shared workers)
  3. Full rebuild (OCS live fetch + LLDP + port groups)

There is also an in-process L1 dict ahead of the Django cache (45 s warm /
120 s cold). Django cache TTL is 45 s warm / 600 s cold. DB snapshots are one
row per (topology, kind) and are valid while ``revision`` matches
:func:`compute_topology_revision`; a cold request may be served a mismatched
("db_stale") snapshot younger than 1 h. Only the ``port_fabric_*`` kinds are
written today; the ``fabric_map_*`` kinds are defined but unused.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# In-process L1 for hot paths (per worker)
_L1_PAYLOAD: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_L1_LOCK = threading.Lock()
_L1_TTL_COLD = 120.0
_L1_TTL_WARM = 45.0

KIND_PORT_FABRIC_COLD = 'port_fabric_cold'
KIND_PORT_FABRIC_WARM = 'port_fabric_warm'
KIND_FABRIC_MAP_COLD = 'fabric_map_cold'
KIND_FABRIC_MAP_WARM = 'fabric_map_warm'

REVISION_PREFIX = 'v1'


def site_files_signature() -> str:
    """Short hash of ocs_photonic_site*.json mtimes (aligns with _site_json_signature)."""
    from pathlib import Path

    parts = []
    site_dir = Path(__file__).resolve().parent.parent / 'resources'
    for p in sorted(site_dir.glob('ocs_photonic_site*.json')):
        try:
            parts.append(f'{p.name}:{int(p.stat().st_mtime)}')
        except OSError:
            continue
    if not parts:
        return '0'
    return hashlib.sha256('|'.join(parts).encode()).hexdigest()[:12]


def compute_topology_revision(topo) -> str:
    """Fingerprint topology + site JSON for snapshot invalidation."""
    try:
        node_count = topo.nodes.count()
        link_count = topo.links.count()
    except Exception:
        node_count = link_count = 0
    ts = topo.updated_at.timestamp() if topo.updated_at else 0
    return f'{REVISION_PREFIX}:{int(ts)}:{node_count}:{link_count}:{site_files_signature()}'


def port_fabric_kind(want_live: bool) -> str:
    """Snapshot kind for Port Fabric: ``warm`` = live OCS, ``cold`` = cached sources."""
    return KIND_PORT_FABRIC_WARM if want_live else KIND_PORT_FABRIC_COLD


def fabric_map_kind(want_live: bool) -> str:
    """Snapshot kind for the node-level Fabric Map (currently unused)."""
    return KIND_FABRIC_MAP_WARM if want_live else KIND_FABRIC_MAP_COLD


def django_cache_key(
    topo_id: int,
    revision: str,
    *,
    api: str,
    want_live: bool,
    want_lldp: bool,
    force_refresh: bool,
) -> str:
    """Django cache key; embeds ``revision`` so topology edits miss naturally."""
    return (
        f'{api}:v8:{topo_id}:{revision}:'
        f'{int(want_live)}:{int(want_lldp)}:{int(force_refresh)}'
    )


def _l1_get(key: str, ttl: float) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _L1_LOCK:
        entry = _L1_PAYLOAD.get(key)
        if not entry:
            return None
        expires, payload = entry
        if expires < now:
            del _L1_PAYLOAD[key]
            return None
        return dict(payload)


def _l1_set(key: str, payload: Dict[str, Any], ttl: float) -> None:
    with _L1_LOCK:
        _L1_PAYLOAD[key] = (time.time() + ttl, dict(payload))


def get_db_snapshot(topo_id: int, kind: str, revision: str) -> Optional[Dict[str, Any]]:
    """Snapshot payload for an exact revision match, with a ``_cache`` metadata block."""
    from .models import LabTopologyFabricSnapshot

    row = (
        LabTopologyFabricSnapshot.objects.filter(
            topology_id=topo_id,
            kind=kind,
            revision=revision,
        )
        .only('payload', 'built_at', 'build_ms', 'meta')
        .first()
    )
    if not row:
        return None
    out = dict(row.payload or {})
    meta = row.meta or {}
    out['_cache'] = {
        'layer': 'db',
        'kind': kind,
        'built_at': row.built_at.isoformat() if row.built_at else None,
        'build_ms': row.build_ms,
        'age_s': round((timezone.now() - row.built_at).total_seconds(), 1) if row.built_at else None,
        **{k: v for k, v in meta.items() if not k.startswith('_')},
    }
    return out


def save_db_snapshot(
    topo_id: int,
    kind: str,
    revision: str,
    payload: Dict[str, Any],
    *,
    build_ms: float = 0,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Upsert the single (topology, kind) snapshot row; drops ``_timing`` from the payload."""
    from .models import LabTopologyFabricSnapshot

    store = {k: v for k, v in payload.items() if k != '_timing'}
    LabTopologyFabricSnapshot.objects.update_or_create(
        topology_id=topo_id,
        kind=kind,
        defaults={
            'revision': revision,
            'payload': store,
            'build_ms': build_ms,
            'meta': meta or {},
        },
    )


def invalidate_topology_fabric_cache(topo_id: Optional[int] = None) -> int:
    """Drop DB snapshots + django cache keys for a topology (or all)."""
    from .models import LabTopologyFabricSnapshot

    deleted = 0
    if topo_id is None:
        deleted, _ = LabTopologyFabricSnapshot.objects.all().delete()
    else:
        deleted, _ = LabTopologyFabricSnapshot.objects.filter(topology_id=topo_id).delete()
        # Best-effort L1 purge
        prefix = f':{topo_id}:'
        with _L1_LOCK:
            for k in list(_L1_PAYLOAD.keys()):
                if prefix in k:
                    del _L1_PAYLOAD[k]
    return deleted


def get_port_fabric_cached(
    topo_id: int,
    revision: str,
    *,
    want_live: bool,
    want_lldp: bool,
    force_refresh: bool,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Return (payload, layer) where layer is 'miss', 'l1', 'django', or 'db'.
    """
    if force_refresh:
        return None, 'miss'

    kind = port_fabric_kind(want_live)
    dkey = django_cache_key(
        topo_id, revision, api='port_fabric',
        want_live=want_live, want_lldp=want_lldp, force_refresh=False,
    )
    l1_key = f'pf:l1:{dkey}'
    ttl = _L1_TTL_WARM if want_live else _L1_TTL_COLD

    hit = _l1_get(l1_key, ttl)
    if hit is not None:
        hit = dict(hit)
        hit.setdefault('_cache', {})['layer'] = 'l1'
        return hit, 'l1'

    hit = cache.get(dkey)
    if hit is not None:
        hit = dict(hit)
        hit.setdefault('_cache', {})['layer'] = 'django'
        _l1_set(l1_key, hit, ttl)
        return hit, 'django'

    hit = get_db_snapshot(topo_id, kind, revision)
    if hit is not None:
        _l1_set(l1_key, hit, ttl)
        cache.set(dkey, {k: v for k, v in hit.items() if k != '_cache'}, ttl)
        return hit, 'db'

    # Stale DB: revision mismatch but recent warm snapshot — serve while rebuilding
    if not want_live:
        from .models import LabTopologyFabricSnapshot

        stale = (
            LabTopologyFabricSnapshot.objects.filter(
                topology_id=topo_id,
                kind=kind,
            )
            .order_by('-built_at')
            .only('payload', 'built_at', 'build_ms', 'meta', 'revision')
            .first()
        )
        if stale and stale.payload:
            age = (timezone.now() - stale.built_at).total_seconds() if stale.built_at else 9999
            if age < 3600:
                out = dict(stale.payload)
                out['_cache'] = {
                    'layer': 'db_stale',
                    'revision': stale.revision,
                    'age_s': round(age, 1),
                    'stale': True,
                }
                return out, 'db_stale'

    return None, 'miss'


def store_port_fabric_cached(
    topo_id: int,
    revision: str,
    payload: Dict[str, Any],
    *,
    want_live: bool,
    want_lldp: bool,
    build_ms: float = 0,
    timing: Optional[Dict[str, Any]] = None,
) -> None:
    """Write a freshly built Port Fabric payload to DB snapshot, Django cache and L1."""
    kind = port_fabric_kind(want_live)
    meta = {'timing': timing or {}}
    save_db_snapshot(topo_id, kind, revision, payload, build_ms=build_ms, meta=meta)

    dkey = django_cache_key(
        topo_id, revision, api='port_fabric',
        want_live=want_live, want_lldp=want_lldp, force_refresh=False,
    )
    store = {k: v for k, v in payload.items() if k not in ('_timing', '_cache')}
    ttl = 45 if want_live else 600
    cache.set(dkey, store, ttl)
    _l1_set(f'pf:l1:{dkey}', payload, _L1_TTL_WARM if want_live else _L1_TTL_COLD)


def schedule_port_fabric_refresh(
    topo_id: int,
    *,
    want_live: bool = True,
    chassis_ssh: bool = False,
) -> None:
    """Background warm/cold rebuild (daemon thread).

    chassis_ssh=True runs per-chassis IxOS LLDP SSH (slow); keep False for API hot path.
    """

    def _run():
        try:
            from django.db import connection

            connection.close()
            from .lab_topology_views import refresh_port_fabric_snapshot

            refresh_port_fabric_snapshot(
                topo_id,
                want_live=want_live,
                want_lldp=True,
                force_refresh=chassis_ssh,
            )
        except Exception as exc:
            logger.warning('background port_fabric refresh topo %s: %s', topo_id, exc)

    threading.Thread(target=_run, daemon=True, name=f'pf-refresh-{topo_id}').start()
