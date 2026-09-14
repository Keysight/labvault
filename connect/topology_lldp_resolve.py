"""Resolve LLDP neighbor records to topology node ids (IPv4 + hostname aliases)."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from .models import KeysightChassis, LabTopology


def lldp_hostname_aliases(name: str) -> Iterable[str]:
    """Yield lookup keys for LLDP system names (short hostname, normalized label)."""
    raw = (name or '').lower().strip().split('.')[0]
    if not raw:
        return
    yield raw
    compact = raw.replace('-', '').replace('_', '').replace(' ', '')
    if compact and compact != raw:
        yield compact


def build_hostname_to_node_map(nodes: Dict[str, dict]) -> Dict[str, str]:
    """Map LLDP hostname aliases → graph node id (in-memory graph ``nodes`` dict)."""
    out: Dict[str, str] = {}
    for node in nodes.values():
        nid = node.get('id')
        if not nid:
            continue
        extra = node.get('extra') if isinstance(node.get('extra'), dict) else {}
        for key in (
            node.get('label') or '',
            node.get('mgmt_display') or '',
            (extra or {}).get('node_key', ''),
        ):
            for alias in lldp_hostname_aliases(str(key)):
                out.setdefault(alias, nid)
    return out


def enrich_hostname_map_from_topo(
    topo: LabTopology,
    host_to_node: Dict[str, str],
    *,
    ip_to_node: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Add hostnames from topology nodes (switches, chassis, OCS, etc.)."""
    for n in topo.nodes.select_related('device').all():
        nid = f'node_{n.pk}'
        extra = n.extra or {}
        ip = ''
        if n.device_id and n.device:
            ip = (n.device.ip_address or '').strip()
            for alias in lldp_hostname_aliases(n.device.hostname or ''):
                host_to_node.setdefault(alias, nid)
        if not ip:
            ip = str(extra.get('device_ip') or '').strip()
        for alias in lldp_hostname_aliases(n.label or ''):
            host_to_node.setdefault(alias, nid)
        if ip and ip_to_node is not None:
            ip_to_node[ip] = nid
        if n.node_type == 'chassis':
            cid = extra.get('chassis_id')
            ch = KeysightChassis.objects.filter(pk=cid).first() if cid else None
            if ch:
                ch_ip = (ch.ip_address or '').strip()
                if ch_ip and ip_to_node is not None:
                    ip_to_node[ch_ip] = nid
                for alias in lldp_hostname_aliases(ch.hostname or ''):
                    host_to_node.setdefault(alias, nid)
    return host_to_node


def resolve_lldp_neighbor_node_id(
    nbr: Dict[str, Any],
    *,
    ip_to_node: Dict[str, str],
    host_to_node: Dict[str, str],
) -> str:
    """Return node id for an LLDP neighbor dict, or '' if unknown."""
    mgmt = (nbr.get('mgmt_ip') or nbr.get('management_address') or '').strip()
    if mgmt:
        nid = ip_to_node.get(mgmt)
        if nid:
            return nid

    remote = (nbr.get('remote_device') or nbr.get('system_name') or '').strip()
    if not remote:
        return ''
    for alias in lldp_hostname_aliases(remote):
        nid = host_to_node.get(alias)
        if nid:
            return nid
    return ''
