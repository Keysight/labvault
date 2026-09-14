"""PCPU / IxOS application version helpers for Lab Pulse fleet view."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional

from django.core.cache import cache

logger = logging.getLogger(__name__)

_IXOS_APP_MARKER = '---LV_IXOS---'
_IXNETWORK_RE = re.compile(r'^ixnetwork', re.I)
_IXOS_RE = re.compile(r'^ixos\b', re.I)

PCPU_APPS_CACHE_TTL = 600


def normalize_ixos_applications(raw: Any) -> Dict[str, Any]:
    """Normalize ixosApplications (list or dict) into a stable payload for the UI."""
    apps: Dict[str, str] = {}
    if isinstance(raw, dict):
        for name, ver in raw.items():
            n = (name or '').strip()
            v = (ver or '').strip()
            if n and v:
                apps[n] = v
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            n = (item.get('name') or '').strip()
            v = (item.get('version') or '').strip()
            if n and v:
                apps[n] = v

    ixos_version = ''
    ixnetwork_version = ''
    for name, ver in apps.items():
        if not ixos_version and _IXOS_RE.search(name):
            ixos_version = ver
        if not ixnetwork_version and _IXNETWORK_RE.search(name):
            ixnetwork_version = ver

    return {
        'ixos_version': ixos_version,
        'ixnetwork_version': ixnetwork_version,
        'applications': apps,
    }


def version_fields_from_chassis(ch) -> Dict[str, Any]:
    """Build version fields from a KeysightChassis ORM row."""
    apps_raw: Any = {}
    if getattr(ch, 'ixos_applications', None):
        try:
            apps_raw = json.loads(ch.ixos_applications or '{}')
        except (TypeError, ValueError):
            apps_raw = {}
    out = normalize_ixos_applications(apps_raw)
    if not out['ixos_version'] and getattr(ch, 'ixos_version', None):
        out['ixos_version'] = (ch.ixos_version or '').strip()
    return out


def parse_pcpu_ixos_blob(output: str) -> Dict[str, Any]:
    """Parse marker-delimited IxOS /chassis JSON from a PCPU SSH hop."""
    blob = output
    if _IXOS_APP_MARKER in output:
        blob = output.split(_IXOS_APP_MARKER, 1)[1].strip()
    blob = blob.strip()
    if not blob:
        return {}
    try:
        data = json.loads(blob)
    except (TypeError, ValueError):
        logger.debug('PCPU ixos blob JSON parse failed (%d bytes)', len(blob))
        return {}
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return {}
    return normalize_ixos_applications(data.get('ixosApplications') or data.get('applications') or data)


def load_pcpu_apps_index(topo_id: int) -> Dict[str, Dict[str, Any]]:
    """mgmt_ip → version payload from collector cache."""
    hit = cache.get(f'pcpu_apps:{topo_id}')
    return dict(hit) if isinstance(hit, dict) else {}


def load_chassis_apps_by_parent(topo_id: int) -> Dict[str, Dict[str, Any]]:
    """parent resource_key (node_N) → chassis-level application versions."""
    from connect.models import KeysightChassis, LabTopologyNode

    out: Dict[str, Dict[str, Any]] = {}
    nodes = LabTopologyNode.objects.filter(topology_id=topo_id).only('pk', 'extra')
    chassis_ids: Dict[int, str] = {}
    for node in nodes:
        cid = (node.extra or {}).get('chassis_id')
        if cid:
            chassis_ids[int(cid)] = f'node_{node.pk}'
    if not chassis_ids:
        return out
    for ch in KeysightChassis.objects.filter(pk__in=list(chassis_ids.keys())):
        parent = chassis_ids.get(ch.pk)
        if parent:
            out[parent] = version_fields_from_chassis(ch)
    return out


def merge_chassis_and_pcpu_versions(
    *,
    parent: str,
    mgmt_ip: str,
    pcpu_apps: Dict[str, Dict[str, Any]],
    chassis_apps_by_parent: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """IxOS stack versions are chassis-wide; prefer KeysightChassis over per-PCPU SSH."""
    chassis_ver = chassis_apps_by_parent.get(parent) or {}
    pcpu_ver = pcpu_apps.get((mgmt_ip or '').strip()) or {}
    if not chassis_ver:
        return dict(pcpu_ver)
    if not pcpu_ver:
        return dict(chassis_ver)

    apps = dict(pcpu_ver.get('applications') or {})
    for name, ver in (chassis_ver.get('applications') or {}).items():
        if _IXOS_RE.search(name) or _IXNETWORK_RE.search(name):
            apps[name] = ver

    return {
        'ixos_version': chassis_ver.get('ixos_version') or pcpu_ver.get('ixos_version') or '',
        'ixnetwork_version': chassis_ver.get('ixnetwork_version') or pcpu_ver.get('ixnetwork_version') or '',
        'applications': apps,
    }


def attach_versions_to_device(
    dev: Dict[str, Any],
    *,
    chassis_apps_by_parent: Dict[str, Dict[str, Any]],
) -> None:
    """Attach chassis-level IxOS / IxNetwork versions to a keysight_chassis device row."""
    parent = (dev.get('resource_key') or '').strip()
    ver = chassis_apps_by_parent.get(parent) or {}
    dev['ixos_version'] = ver.get('ixos_version') or ''
    dev['ixnetwork_version'] = ver.get('ixnetwork_version') or ''
    dev['applications'] = dict(ver.get('applications') or {})


def attach_versions_to_pcpu_group(
    grp: Dict[str, Any],
    *,
    pcpu_apps: Dict[str, Dict[str, Any]],
    chassis_apps_by_parent: Dict[str, Dict[str, Any]],
) -> None:
    """Mutate a PCPU group dict with ixos/ixnetwork/application versions."""
    ver = merge_chassis_and_pcpu_versions(
        parent=grp.get('parent') or '',
        mgmt_ip=grp.get('mgmt_ip') or '',
        pcpu_apps=pcpu_apps,
        chassis_apps_by_parent=chassis_apps_by_parent,
    )
    grp['ixos_version'] = ver.get('ixos_version') or ''
    grp['ixnetwork_version'] = ver.get('ixnetwork_version') or ''
    grp['applications'] = dict(ver.get('applications') or {})
