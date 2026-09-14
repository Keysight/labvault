"""
Split a full HBG / OCS datacenter topology into staged sub-topologies.

- **without_ocs_patch**: AresONE M01–M04 + Arista switches, DAC/B2B only (no OCS node).
- **with_ocs_patch**: AresONE M05–M08 + OCS + servers + Arista, live OCS patch stage.

Parent topology ``extra.sub_topologies`` references the children; children store
``parent_topology_id`` and ``sub_topology_role``.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from django.db import transaction

from connect.lab_topology_io import export_topology, import_topology
from connect.models import LabTopology

WITHOUT_OCS_NODE_KEYS: FrozenSet[str] = frozenset({
    'aresone01', 'aresone02', 'aresone03', 'aresone04',
    'arista1', 'arista2', 'arista3', 'arista4',
})
WITH_OCS_NODE_KEYS: FrozenSet[str] = frozenset({
    'aresone05', 'aresone06', 'aresone07', 'aresone08',
    'ocs', 'server01', 'server02',
    'arista1', 'arista2', 'arista3', 'arista4',
})

_SUB_ROLE_WITHOUT = 'without_ocs_patch'
_SUB_ROLE_WITH = 'with_ocs_patch'


def _node_key(nd: Dict[str, Any]) -> str:
    return str(nd.get('node_key') or nd.get('id') or '').strip()


def _strip_ocs_from_node_extra(extra: Dict[str, Any]) -> Dict[str, Any]:
    """Remove OCS-stage ports from chassis/switch extras for DAC-only staging."""
    ex = copy.deepcopy(extra or {})
    ex.pop('ocs_physical_ports', None)
    ocs_names = set()
    details = []
    for p in ex.get('port_details') or []:
        if not isinstance(p, dict):
            continue
        if (p.get('role') or '').lower() == 'ocs':
            if p.get('name'):
                ocs_names.add(str(p['name']))
            continue
        details.append(p)
    ex['port_details'] = details
    ports = []
    for p in ex.get('ports') or []:
        pname = p if isinstance(p, str) else (p.get('name') or p.get('label') or '')
        if pname and pname in ocs_names:
            continue
        if isinstance(pname, str) and re.match(r'^port_[1-8]$', pname):
            continue
        ports.append(p)
    ex['ports'] = ports
    sl = ex.get('slot_layout')
    if isinstance(sl, list):
        ex['slot_layout'] = _filter_slot_layout_ocs(sl)
    elif isinstance(sl, dict) and sl.get('slots'):
        ex['slot_layout'] = {
            **sl,
            'slots': _filter_slot_layout_ocs(sl.get('slots') or []),
        }
    ex['ocs_stage_enabled'] = False
    return ex


def _filter_slot_layout_ocs(slots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        slot = copy.deepcopy(slot)
        rgs = []
        for rg in slot.get('resource_groups') or []:
            ports = [
                p for p in (rg.get('ports') or [])
                if (p.get('role') or '').lower() != 'ocs'
            ]
            if ports:
                rg = {**rg, 'ports': ports}
                rgs.append(rg)
        slot['resource_groups'] = rgs
        slot['ports'] = [
            p for p in (slot.get('ports') or [])
            if (p.get('role') or '').lower() != 'ocs'
        ]
        out.append(slot)
    return out


def _filter_export_payload(
    payload: Dict[str, Any],
    *,
    keep_keys: FrozenSet[str],
    strip_ocs_ports: bool,
    sub_role: str,
) -> Dict[str, Any]:
    out = copy.deepcopy(payload)
    nodes = [nd for nd in (out.get('nodes') or []) if _node_key(nd) in keep_keys]
    if strip_ocs_ports:
        for nd in nodes:
            extra = dict(nd.get('extra') or {})
            nd['extra'] = _strip_ocs_from_node_extra(extra)
            if nd.get('ports'):
                nd['ports'] = list((nd['extra'] or {}).get('ports') or [])
    kept = {_node_key(nd) for nd in nodes}
    links = []
    for lk in out.get('links') or []:
        a = str(lk.get('node_a') or '').strip()
        b = str(lk.get('node_b') or '').strip()
        if a not in kept or b not in kept:
            continue
        if strip_ocs_ports and (lk.get('cable_type') or '') in ('ocs',):
            continue
        links.append(lk)
    out['nodes'] = nodes
    out['links'] = links
    topo_meta = dict(out.get('topology') or {})
    extra = dict(topo_meta.get('extra') or {})
    extra['sub_topology_role'] = sub_role
    extra['ocs_stage_enabled'] = sub_role == _SUB_ROLE_WITH
    topo_meta['extra'] = extra
    out['topology'] = topo_meta
    return out


def _unique_topo_name(base: str) -> str:
    name = base
    n = 2
    while LabTopology.objects.filter(name=name).exists():
        name = f'{base} ({n})'
        n += 1
    return name


def _create_sub_topology(
    parent: LabTopology,
    payload: Dict[str, Any],
    *,
    name: str,
    sub_role: str,
    tags: str,
) -> LabTopology:
    topo_meta = dict(payload.get('topology') or {})
    if not isinstance(topo_meta, dict):
        topo_meta = {}
    topo_meta['name'] = name
    extra = dict(topo_meta.get('extra') or {})
    extra['parent_topology_id'] = parent.pk
    extra['sub_topology_role'] = sub_role
    extra['ocs_stage_enabled'] = sub_role == _SUB_ROLE_WITH
    parent_extra = dict(parent.extra or {})
    if parent_extra.get('addressing_profile'):
        extra['addressing_profile'] = parent_extra['addressing_profile']
    if parent_extra.get('site_json'):
        extra['site_json'] = parent_extra['site_json']
    topo_meta['extra'] = extra
    payload['topology'] = topo_meta

    topo = LabTopology.objects.create(
        name=name,
        description=(
            f'Sub-topology of «{parent.name}»: '
            + ('OCS patched stage (M05–M08 + OCS)' if sub_role == _SUB_ROLE_WITH
               else 'DAC staging without OCS (M01–M04)')
        ),
        source='split',
        tags=tags,
        extra={},
        created_by=parent.created_by,
    )
    import_topology(topo, payload)
    return topo


@transaction.atomic
def split_hbg_ocs_sub_topologies(
    parent: LabTopology,
    *,
    replace_existing: bool = True,
) -> Dict[str, LabTopology]:
    """
    Create or refresh the two HBG OCS-stage sub-topologies from ``parent``.

    Returns ``{'without_ocs_patch': topo, 'with_ocs_patch': topo}``.
    """
    payload = export_topology(parent)
    parent_extra = dict(parent.extra or {})
    existing = dict(parent_extra.get('sub_topologies') or {})

    if replace_existing:
        for entry in existing.values():
            tid = (entry or {}).get('id')
            if tid:
                LabTopology.objects.filter(pk=tid).delete()

    without_name = _unique_topo_name(f'{parent.name} — DAC staging (no OCS)')
    with_name = _unique_topo_name(f'{parent.name} — OCS patched')

    without_payload = _filter_export_payload(
        payload,
        keep_keys=WITHOUT_OCS_NODE_KEYS,
        strip_ocs_ports=True,
        sub_role=_SUB_ROLE_WITHOUT,
    )
    with_payload = _filter_export_payload(
        payload,
        keep_keys=WITH_OCS_NODE_KEYS,
        strip_ocs_ports=False,
        sub_role=_SUB_ROLE_WITH,
    )

    without_topo = _create_sub_topology(
        parent, without_payload,
        name=without_name,
        sub_role=_SUB_ROLE_WITHOUT,
        tags='hbg,sub-topology,dac-staging,no-ocs',
    )
    with_topo = _create_sub_topology(
        parent, with_payload,
        name=with_name,
        sub_role=_SUB_ROLE_WITH,
        tags='hbg,sub-topology,ocs-patched',
    )

    parent_extra['sub_topologies'] = {
        _SUB_ROLE_WITHOUT: {
            'id': without_topo.pk,
            'name': without_topo.name,
            'label': 'DAC staging (M01–M04, no OCS)',
            'chassis': ['aresone01', 'aresone02', 'aresone03', 'aresone04'],
        },
        _SUB_ROLE_WITH: {
            'id': with_topo.pk,
            'name': with_topo.name,
            'label': 'OCS patched (M05–M08 + OCS)',
            'chassis': ['aresone05', 'aresone06', 'aresone07', 'aresone08'],
        },
    }
    parent_extra['topology_family'] = 'hbg_ocs_split'
    parent.extra = parent_extra
    parent.save(update_fields=['extra', 'updated_at'])

    return {
        _SUB_ROLE_WITHOUT: without_topo,
        _SUB_ROLE_WITH: with_topo,
    }


def get_sub_topology_nav(topo: LabTopology) -> Dict[str, Any]:
    """Context for templates: parent link and sibling sub-topologies."""
    extra = dict(topo.extra or {})
    parent_id = extra.get('parent_topology_id')
    subs = dict(extra.get('sub_topologies') or {})
    role = extra.get('sub_topology_role', '')
    parent = LabTopology.objects.filter(pk=parent_id).first() if parent_id else None
    siblings = []
    if parent:
        subs = dict((parent.extra or {}).get('sub_topologies') or {})
    for key, entry in subs.items():
        if not isinstance(entry, dict):
            continue
        siblings.append({
            'role': key,
            'id': entry.get('id'),
            'name': entry.get('name') or entry.get('label') or key,
            'label': entry.get('label') or entry.get('name') or key,
            'active': bool(entry.get('id') == topo.pk or key == role),
        })
    return {
        'parent_topology': parent,
        'sub_topology_role': role,
        'sub_topologies': siblings,
        'is_sub_topology': bool(parent_id),
        'is_parent_with_subs': bool(subs) and not parent_id,
    }
