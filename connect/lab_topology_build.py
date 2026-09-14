"""
Build a LabTopology from current DB: devices, chassis, TopologyLink, ChassisDeviceLink.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction

from connect.models import (
    ChassisDeviceLink,
    Device,
    KeysightChassis,
    LabTopology,
    LabTopologyLink,
    LabTopologyNode,
    TopologyLink,
)
from connect.topology import CHASSIS_NODE_PREFIX, get_cached_topology


def _parse_ocs_mapping(notes: str) -> dict:
    if not notes:
        return {}
    m = re.search(r'\[ocs_site_mapping\]\s*(\{.*\})', notes, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def _infer_node_type(vendor: str, node_id: str) -> str:
    if vendor == 'ocs':
        return 'ocs'
    if str(node_id).startswith(CHASSIS_NODE_PREFIX):
        return 'chassis'
    if vendor in ('arista', 'sonic', 'fortigate', 'paloalto'):
        return 'switch'
    return 'other'


def _infer_cable(port_a: str, port_b: str) -> str:
    for s in ((port_a or ''), (port_b or '')):
        s2 = s.strip()
        if re.match(r'^\d+\.\d+', s2):
            return 'optic'
    pa, pb = (port_a or '').lower(), (port_b or '').lower()
    if 'eth' in pa or 'ethernet' in pa or 'eth' in pb or 'ethernet' in pb or 'port_' in pa:
        return 'dac'
    return 'direct'


def _ports_for_device(device_id: int) -> List[str]:
    ports = set()
    for tl in TopologyLink.objects.filter(device_a_id=device_id).only('port_a'):
        if tl.port_a:
            ports.add(tl.port_a)
    for tl in TopologyLink.objects.filter(device_b_id=device_id).only('port_b'):
        if tl.port_b:
            ports.add(tl.port_b)
    for cl in ChassisDeviceLink.objects.filter(device_id=device_id).only('port_device'):
        if cl.port_device:
            ports.add(cl.port_device)
    return sorted(ports)[:80]


def _cable_choices(t: str) -> str:
    if t == 'dac':
        return 'dac'
    if t == 'optic':
        return 'optic'
    return 'direct'


def _color_for_cable(t: str) -> str:
    if t == 'dac':
        return 'red'
    if t == 'optic':
        return 'green'
    return 'blue'


def _grid(i: int, ncols: int = 4, dx: float = 240.0, dy: float = 200.0) -> Tuple[float, float]:
    return 40.0 + (i % ncols) * dx, 40.0 + (i // ncols) * dy


@transaction.atomic
def build_lab_topology_from_cache(
    name: str,
    user,
    description: str = '',
    tags: str = '',
) -> LabTopology:
    """Create LabTopology + nodes from get_cached nodes; links from ORM (not aggregated)."""
    data = get_cached_topology()
    nodes = data.get('nodes') or []
    topo = LabTopology.objects.create(
        name=name or 'From LLDP',
        description=description,
        source='lldp',
        tags=tags,
        created_by=user if user and getattr(user, 'is_authenticated', False) else None,
    )
    id_to_key: Dict[str, str] = {}
    key_to_ltn: Dict[str, LabTopologyNode] = {}
    for i, n in enumerate(nodes):
        nid = str(n['id'])
        node_key = f"n_{nid}" if not nid.startswith(CHASSIS_NODE_PREFIX) else f"k_{nid.replace(CHASSIS_NODE_PREFIX, '')}"
        vendor = n.get('vendor', '') or ''
        ntype = _infer_node_type(vendor, nid)
        x, y = _grid(i)
        extra: Dict[str, Any] = {}
        dev = None
        if nid.startswith(CHASSIS_NODE_PREFIX):
            try:
                cid = int(nid.replace(CHASSIS_NODE_PREFIX, ''))
                if KeysightChassis.objects.filter(id=cid).exists():
                    extra['chassis_id'] = cid
            except ValueError:
                pass
        else:
            try:
                did = int(nid)
                dev = Device.objects.filter(id=did).first()
                if dev:
                    extra['ports'] = _ports_for_device(did)
                    om = _parse_ocs_mapping(dev.notes or '')
                    if om:
                        extra['ocs_site_mapping'] = om
            except ValueError:
                pass
        ltn = LabTopologyNode.objects.create(
            topology=topo,
            node_key=node_key,
            device=dev,
            node_type=ntype,
            label=n.get('name') or n.get('ip') or node_key,
            x=x, y=y,
            extra=extra,
        )
        id_to_key[nid] = node_key
        key_to_ltn[node_key] = ltn

    for tl in TopologyLink.objects.select_related('device_a', 'device_b').all():
        sa, sb = str(tl.device_a_id), str(tl.device_b_id)
        if sa not in id_to_key or sb not in id_to_key:
            continue
        la, lb = id_to_key[sa], id_to_key[sb]
        if la not in key_to_ltn or lb not in key_to_ltn:
            continue
        na, nb = key_to_ltn[la], key_to_ltn[lb]
        ct = _infer_cable(tl.port_a, tl.port_b)
        LabTopologyLink.objects.create(
            topology=topo,
            node_a=na,
            port_a=tl.port_a or '?',
            node_b=nb,
            port_b=tl.port_b or '?',
            cable_type=_cable_choices(ct),
            color=_color_for_cable(ct),
            label=tl.lag or '',
        )

    for cl in ChassisDeviceLink.objects.select_related('chassis', 'device').filter(link_status='up'):
        sa = f"{CHASSIS_NODE_PREFIX}{cl.chassis_id}"
        sb = str(cl.device_id)
        if sa not in id_to_key or sb not in id_to_key:
            continue
        la, lb = id_to_key[sa], id_to_key[sb]
        na, nb = key_to_ltn[la], key_to_ltn[lb]
        ct = _infer_cable(cl.port_chassis, cl.port_device)
        LabTopologyLink.objects.create(
            topology=topo,
            node_a=na,
            port_a=cl.port_chassis or '?',
            node_b=nb,
            port_b=cl.port_device or '?',
            cable_type=_cable_choices(ct),
            color=_color_for_cable(ct),
            label='',
        )
    return topo
