"""Build IP/hostname → node id maps for topology graphs (IPv4 + IPv6)."""
from __future__ import annotations

from typing import Dict

from .ip_addressing import identity_address_keys, normalize_ip


def build_ip_to_node_map(nodes: Dict[str, dict]) -> Dict[str, str]:
    """
    Map management addresses (v4, v6, mgmt_display) to graph node ids.
    """
    out: Dict[str, str] = {}
    for node in nodes.values():
        nid = node.get('id')
        if not nid:
            continue
        ipv4 = (node.get('mgmt_ipv4') or '').strip()
        ipv6 = normalize_ip(node.get('mgmt_ipv6') or '')
        disp = (node.get('mgmt_display') or '').strip()
        label = (node.get('label') or '').strip()
        for key in identity_address_keys(ipv4=ipv4, ipv6=ipv6, hostname=label):
            out.setdefault(key, nid)
        if disp:
            out.setdefault(disp, nid)
            low = disp.lower()
            if low != disp:
                out.setdefault(low, nid)
    return out
