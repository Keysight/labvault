"""APS standalone topology: single KCOS node running mgmt + CN on one box.

Distinct from multi-node M1010/M8400 chassis where mgmt (role master/merlin) is
separate from cn-aps-* compute nodes. Standalone units only serve themselves (1 CN).
"""

from __future__ import annotations

import re

from .keysight_drivers import KCOS_TYPES

# KCOS types that can appear as standalone mgmt+CN (not T-Rex / HTREX).
_STANDALONE_ELIGIBLE = {'aps_m1010', 'aps_m8400', 'aps_standalone'}

# Hostname prefixes for boxes that are typically standalone mgmt+CN appliances.
_STANDALONE_HOSTNAME_RE = re.compile(
    r'^aps-(o[12]|m[12])-',
    re.IGNORECASE,
)


def is_mgmt_kcos_role(role: str) -> bool:
    return (role or '').lower() in ('merlin', 'master')


def is_standalone_kcos_api_nodes(nodes: list[dict] | None) -> bool:
    """Detect combined mgmt+CN from raw /introspection/nodes payload."""
    if not nodes or len(nodes) != 1:
        return False
    n = nodes[0]
    return (
        (n.get('name') or '').strip() == 'mgmt'
        and (n.get('role') or '').lower() == 'compute'
    )


def is_standalone_node_inventory(nodes: list[dict] | None) -> bool:
    """Detect standalone from get_node_inventory() shaped rows."""
    if not nodes or len(nodes) != 1:
        return False
    n = nodes[0]
    if n.get('operating_mode') == 'standalone_merged':
        return True
    if (n.get('node_name') or '').strip() != 'mgmt':
        return False
    kcos_role = (n.get('kcos_role') or '').lower()
    if kcos_role == 'compute':
        return True
    return n.get('role') == 'Standalone'


def standalone_aps_generation(hostname: str, node_name: str = '') -> str | None:
    """APS 1.0 (O1/M1) vs 1.5 (O2) from hostname when node names are generic."""
    hn = (hostname or '').upper()
    if hn.startswith('APS-O2-') or 'APS-O2' in hn:
        return '15'
    if hn.startswith(('APS-O1-', 'APS-M1-', 'APS-M2-')):
        return '10'
    nn = (node_name or '').lower()
    if 'cn-aps-o2-' in nn or 'cn-aps-o15-' in nn:
        return '15'
    if nn.startswith('cn-aps-') or nn.startswith('cn-'):
        return '10'
    return None


def is_standalone_hostname(hostname: str) -> bool:
    """True for APS-O1/O2/M1/M2 appliance hostnames (typical standalone boxes)."""
    return bool(_STANDALONE_HOSTNAME_RE.match((hostname or '').strip()))


def standalone_cn_identity(hostname: str) -> str:
    """Canonical CN node name for a standalone appliance hostname.

    APS-O2-SG25341002 → cn-aps-o2-sg25341002
  cn-aps-o2-sg25341005 → cn-aps-o2-sg25341005 (unchanged)
    """
    hn = (hostname or '').strip()
    if not hn:
        return ''
    low = hn.lower()
    if low.startswith('cn-aps-') or low.startswith('cn-'):
        return low
    m = re.match(r'^aps-(o[12]|m[12])-(.+)$', low, re.IGNORECASE)
    if not m:
        return low
    gen_part = m.group(1).lower()
    serial = m.group(2).lower()
    if gen_part.startswith('o') and len(gen_part) == 2:
        return f'cn-aps-o{gen_part[1:]}-{serial}'
    return f'cn-aps-{gen_part}-{serial}'


def standalone_chassis_index(chassis_list) -> dict[str, object]:
    """Map canonical CN name → chassis for cross-linking CN slots vs standalone boxes."""
    index: dict[str, object] = {}
    for ch in chassis_list:
        cn_id = standalone_cn_identity(ch.hostname or ch.ip_address or '')
        if cn_id:
            index[cn_id] = ch
    return index


def standalone_gen_for_chassis(ch, assoc: dict | None = None) -> str | None:
    """APS 1.0 / 1.5 for a standalone chassis row."""
    if assoc and assoc.get('mgmt_node'):
        gen = assoc['mgmt_node'].get('aps_gen')
        if gen in ('10', '15'):
            return gen
    if ch is not None:
        return standalone_aps_generation(getattr(ch, 'hostname', '') or '', '')
    return None


def infer_chassis_type_from_kcos(
    *,
    is_standalone: bool,
    chart_name: str = '',
    hostname: str = '',
    bps_model: str = '',
    current_type: str = '',
) -> str:
    """Pick chassis_type after KCOS introspection."""
    if current_type in ('aresone_htrex', 'trex'):
        return current_type
    if is_standalone:
        return 'aps_standalone'
    chart = (chart_name or '').lower()
    host = (hostname or '').lower()
    model = (bps_model or '').lower()
    if 'kcos-400' in chart or 'eagle-merlin' in chart or 'm8400' in model:
        return 'aps_m8400'
    if 'm1010' in model or 'm1020' in model:
        return 'aps_m1010'
    if chart or 'm1010' in host:
        return 'aps_m1010'
    if 'm8400' in host:
        return 'aps_m8400'
    if current_type in KCOS_TYPES:
        return current_type
    return 'aps_m1010'


def bmc_ips_from_network_addresses(node: dict) -> list[str]:
    """Extract BMC IPs from introspection node bmcNetworkAddresses."""
    addrs = node.get('bmcNetworkAddresses') or {}
    ips: list[str] = []
    for _ch, cidrs in addrs.items():
        for cidr in (cidrs or []):
            part = (cidr or '').split('/')[0].strip()
            if part and part not in ips:
                ips.append(part)
    return ips


def primary_bmc_ip(node: dict) -> str:
    """Prefer ipmi-ch1, else first address from bmcNetworkAddresses."""
    addrs = node.get('bmcNetworkAddresses') or {}
    for key in ('ipmi-ch1', 'ipmi-ch7', 'ipmi-ch8', 'ipmi-ch9'):
        for cidr in (addrs.get(key) or []):
            part = (cidr or '').split('/')[0].strip()
            if part:
                return part
    ips = bmc_ips_from_network_addresses(node)
    return ips[0] if ips else ''


def node_operating_mode(kcos_role: str, node_name: str, is_standalone: bool) -> str:
    if is_standalone:
        return 'standalone_merged'
    if is_mgmt_kcos_role(kcos_role):
        return 'chassis_mgmt'
    if (node_name or '').startswith('cn-') or 'cn-aps-' in (node_name or '').lower():
        return 'chassis_cn'
    return 'unknown'


def chassis_type_eligible_for_standalone(chassis_type: str) -> bool:
    return chassis_type in _STANDALONE_ELIGIBLE
