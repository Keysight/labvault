"""
Discover DAC and back-to-back links by matching transceiver serial numbers.

When two different ports share the same non-empty serial (IxOS / Keysight cache,
Arista DOM, or port_details on topology nodes), they are treated as physically
connected — DAC across devices, direct/B2B within the same chassis.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .models import Device, KeysightChassis, LabTopology, LabTopologyLink, LabTopologyNode

logger = logging.getLogger(__name__)

_SKIP_SERIALS = frozenset({'', 'N/A', 'NA', 'NONE', 'UNKNOWN', '-', '0'})


def normalize_serial(value: Any) -> str:
    s = (str(value or '')).strip().upper()
    if not s or s in _SKIP_SERIALS:
        return ''
    return s


def port_label_from_record(port: Dict[str, Any]) -> str:
    """Stable port name for topology links."""
    if port.get('name'):
        return str(port['name'])
    pn = port.get('port_number')
    slot = port.get('card_number') or port.get('slot')
    if pn is not None and str(pn).strip() != '':
        if slot is not None and str(slot).strip() != '':
            return f'c{slot}p{pn}'
        return f'port_{pn}'
    return str(port.get('id') or 'port')


def _port_entry(
    *,
    port: str,
    serial: str,
    node: Optional[LabTopologyNode] = None,
    chassis: Optional[KeysightChassis] = None,
    device: Optional[Device] = None,
    source: str = '',
) -> Dict[str, Any]:
    ip = ''
    label = ''
    node_type = 'generic'
    node_id = None
    chassis_id = None
    device_id = None
    if node:
        node_id = node.pk
        label = node.label
        node_type = node.node_type
        extra = node.extra or {}
        chassis_id = extra.get('chassis_id')
        ip = (node.device.ip_address if node.device else '') or extra.get('device_ip', '')
    if chassis:
        chassis_id = chassis.pk
        ip = chassis.ip_address or ip
        label = label or chassis.hostname or ip
        node_type = 'chassis'
    if device:
        device_id = device.pk
        ip = device.ip_address or ip
        label = label or device.hostname or ip
        node_type = _vendor_node_type(device.vendor_type)

    return {
        'node_id': node_id,
        'chassis_id': chassis_id,
        'device_id': device_id,
        'ip': ip,
        'label': label,
        'node_type': node_type,
        'port': port,
        'serial': serial,
        'source': source,
    }


_NODE_TYPE_MAP = {
    'arista': 'switch',
    'sonic': 'switch',
    'keysight': 'chassis',
    'ocs': 'ocs',
    'fortigate': 'firewall',
    'paloalto': 'firewall',
}


def _vendor_node_type(vendor: str) -> str:
    return _NODE_TYPE_MAP.get(vendor or '', 'switch')


def _iter_cached_chassis_ports(cached: Dict[str, Any]):
    """Walk IxOS cards/RGs and flat port lists (XGS12, AresONE, etc.)."""
    seen: set = set()
    for card in cached.get('cards') or []:
        if not isinstance(card, dict):
            continue
        card_num = card.get('card_number')
        for rg in card.get('resource_groups') or []:
            for port in rg.get('ports') or []:
                if isinstance(port, dict):
                    p = dict(port)
                    if card_num is not None and p.get('card_number') is None:
                        p['card_number'] = card_num
                    key = (card_num, p.get('port_number'), p.get('name'))
                    if key not in seen:
                        seen.add(key)
                        yield p
        for port in card.get('ports') or []:
            if isinstance(port, dict):
                p = dict(port)
                if card_num is not None and p.get('card_number') is None:
                    p['card_number'] = card_num
                key = (card_num, p.get('port_number'), p.get('name'))
                if key not in seen:
                    seen.add(key)
                    yield p
    for port in cached.get('ports') or []:
        if isinstance(port, dict):
            yield port


def collect_chassis_port_serials(
    chassis: KeysightChassis,
    *,
    refresh: bool = False,
) -> List[Dict[str, Any]]:
    """Port records with serials from IxOS/Keysight cache (optional live refresh)."""
    from .keysight_views import _get_cached, fetch_chassis_data

    cached = None
    if refresh:
        try:
            cached = fetch_chassis_data(chassis)
        except Exception as exc:
            logger.debug('refresh chassis %s failed: %s', chassis.ip_address, exc)
    if not cached:
        cached = _get_cached(chassis.id)
    if not cached:
        return []

    cache_source = 'ixos_cache' if (chassis.chassis_type or '') not in ('kcos',) else 'kcos_cache'
    out: List[Dict[str, Any]] = []
    for port in _iter_cached_chassis_ports(cached):
        serial = normalize_serial(port.get('transceiver_serial'))
        if not serial:
            continue
        out.append(_port_entry(
            port=port_label_from_record(port),
            serial=serial,
            chassis=chassis,
            source=cache_source,
        ))
    return out


def collect_device_port_serials(device: Device) -> List[Dict[str, Any]]:
    """Arista (and similar) transceiver serials via driver DOM/eAPI."""
    if device.vendor_type not in ('arista', 'sonic'):
        return []
    try:
        from .drivers import get_driver
        drv = get_driver(device)
        if not hasattr(drv, 'get_dom_info'):
            return []
        result = drv.get_dom_info()
        if not result.success:
            return []
    except Exception as exc:
        logger.debug('device dom %s: %s', device.ip_address, exc)
        return []

    out: List[Dict[str, Any]] = []
    for row in result.data or []:
        if not isinstance(row, dict):
            continue
        serial = normalize_serial(
            row.get('serial')
            or row.get('vendorSn')
            or row.get('vendor_sn')
            or row.get('vendor')
        )
        if not serial:
            continue
        iface = row.get('interface') or row.get('name') or ''
        if not iface:
            continue
        port = iface if iface.startswith('port_') else (
            f'port_{iface}' if re.match(r'^Ethernet?\d', iface, re.I) else iface
        )
        out.append(_port_entry(
            port=port,
            serial=serial,
            device=device,
            source='switch_dom',
        ))
    return out


def collect_topology_port_serials(topo: LabTopology) -> List[Dict[str, Any]]:
    """Serials stored on nodes (extra.port_details) only."""
    entries: List[Dict[str, Any]] = []
    for node in topo.nodes.select_related('device').all():
        extra = node.extra or {}
        for pd in extra.get('port_details') or []:
            if not isinstance(pd, dict):
                continue
            serial = normalize_serial(pd.get('serial') or pd.get('transceiver_serial'))
            if not serial:
                continue
            entries.append(_port_entry(
                port=pd.get('name') or port_label_from_record(pd),
                serial=serial,
                node=node,
                source='topo_port_details',
            ))
    return entries


def find_serial_pairs(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Pair ports that share a transceiver serial on different port names.
    Returns link dicts ready for apply_serial_links_to_topology.
    """
    by_serial: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ent in entries:
        serial = ent.get('serial') or ''
        if not serial:
            continue
        key = (ent.get('node_id'), ent.get('port'))
        # dedupe same node+port
        if any((e.get('node_id'), e.get('port')) == key for e in by_serial[serial]):
            continue
        by_serial[serial].append(ent)

    pairs: List[Dict[str, Any]] = []
    for serial, group in by_serial.items():
        if len(group) < 2:
            continue
        # Pairwise: for N>2, connect as chain or all-pairs? User asked for pairs of 2 ports.
        if len(group) == 2:
            combos = [(group[0], group[1])]
        else:
            # Multiple ports with same serial (breakout / MPO): pair consecutive sorted by label
            sorted_g = sorted(group, key=lambda x: (x.get('label', ''), x.get('port', '')))
            combos = list(zip(sorted_g, sorted_g[1:]))
        for a, b in combos:
            if a.get('port') == b.get('port') and a.get('node_id') == b.get('node_id'):
                continue
            cable_type, link_kind = _classify_pair(a, b)
            pairs.append({
                'serial': serial,
                'node_a': a.get('node_id'),
                'port_a': a.get('port'),
                'node_b': b.get('node_id'),
                'port_b': b.get('port'),
                'label_a': a.get('label'),
                'label_b': b.get('label'),
                'cable_type': cable_type,
                'link_kind': link_kind,
                'source': 'serial_match',
            })
    return pairs


def _classify_pair(a: Dict, b: Dict) -> Tuple[str, str]:
    """Return (cable_type, link_kind) for a serial-matched pair."""
    ta, tb = a.get('node_type'), b.get('node_type')
    same_chassis = (
        a.get('chassis_id') and a.get('chassis_id') == b.get('chassis_id')
    ) or (ta == 'chassis' and tb == 'chassis' and a.get('node_id') == b.get('node_id'))
    if same_chassis:
        return 'direct', 'b2b'
    if ta == 'chassis' and tb == 'chassis':
        return 'direct', 'b2b'
    if ta == 'switch' and tb == 'switch':
        return 'dac', 'dac'
    return 'dac', 'dac'


def discover_serial_links(
    topo: LabTopology,
    *,
    refresh_cache: bool = False,
) -> Dict[str, Any]:
    """Scan topology nodes and return proposed serial-based links."""
    entries: List[Dict[str, Any]] = []
    for node in topo.nodes.select_related('device').all():
        extra = node.extra or {}
        cid = extra.get('chassis_id')
        if cid and node.node_type == 'chassis':
            ch = KeysightChassis.objects.filter(pk=cid).first()
            if ch:
                for e in collect_chassis_port_serials(ch, refresh=refresh_cache):
                    e['node_id'] = node.pk
                    e['label'] = node.label
                    entries.append(e)
        elif node.device_id and node.device:
            for e in collect_device_port_serials(node.device):
                e['node_id'] = node.pk
                e['label'] = node.label
                entries.append(e)
    entries.extend(collect_topology_port_serials(topo))

    # Dedupe
    seen = set()
    unique: List[Dict[str, Any]] = []
    for e in entries:
        k = (e.get('node_id'), e.get('port'), e.get('serial'))
        if k in seen:
            continue
        seen.add(k)
        unique.append(e)

    pairs = find_serial_pairs(unique)
    return {
        'port_entries': len(unique),
        'pairs': pairs,
        'dac_count': sum(1 for p in pairs if p.get('link_kind') == 'dac'),
        'b2b_count': sum(1 for p in pairs if p.get('link_kind') == 'b2b'),
    }


def _link_exists(topo: LabTopology, na_id: int, pa: str, nb_id: int, pb: str) -> bool:
    pa, pb = pa or '', pb or ''
    for lk in topo.links.filter(node_a_id__in=[na_id, nb_id], node_b_id__in=[na_id, nb_id]):
        if {lk.node_a_id, lk.node_b_id} == {na_id, nb_id}:
            if (lk.port_a == pa and lk.port_b == pb) or (lk.port_a == pb and lk.port_b == pa):
                return True
            if not pa and not pb and not lk.port_a and not lk.port_b:
                return True
    return False


def apply_serial_links_to_topology(
    topo: LabTopology,
    pairs: List[Dict[str, Any]],
    *,
    skip_existing: bool = True,
) -> Dict[str, int]:
    """Create LabTopologyLink rows from discover_serial_links pairs."""
    cable_colors = {
        'dac': '#ef4444',
        'optic': '#22c55e',
        'direct': '#3b82f6',
        'ocs': '#fb923c',
    }

    created = 0
    skipped = 0
    for p in pairs:
        na_id = p.get('node_a')
        nb_id = p.get('node_b')
        if not na_id or not nb_id or na_id == nb_id:
            skipped += 1
            continue
        pa = p.get('port_a') or ''
        pb = p.get('port_b') or ''
        if skip_existing and _link_exists(topo, na_id, pa, nb_id, pb):
            skipped += 1
            continue
        cable = p.get('cable_type') or 'dac'
        serial = p.get('serial') or ''
        label = f'{p.get("link_kind", "dac").upper()} serial {serial[:12]}'
        LabTopologyLink.objects.create(
            topology=topo,
            node_a_id=na_id,
            port_a=pa,
            node_b_id=nb_id,
            port_b=pb,
            cable_type=cable,
            color=cable_colors.get(cable, '#888'),
            label=label,
            extra={'discovery': 'serial_match', 'serial': serial, 'link_kind': p.get('link_kind')},
        )
        created += 1
    topo.save(update_fields=['updated_at'])
    return {'created': created, 'skipped': skipped}


def ports_from_chassis_and_site(
    chassis: KeysightChassis,
    site_entry: Optional[Dict] = None,
    *,
    refresh: bool = False,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """
    Build ports list (string names for designer UI) and port_details (with serials).
    Merges IxOS/Keysight cache ports with site JSON OCS triplets.
    """
    port_names: List[str] = []
    details: List[Dict[str, Any]] = []
    seen_names: set = set()

    for e in collect_chassis_port_serials(chassis, refresh=refresh):
        name = e['port']
        if name in seen_names:
            continue
        seen_names.add(name)
        port_names.append(name)
        details.append({
            'name': name,
            'serial': e['serial'],
            'role': 'dac',
            'source': e.get('source', ''),
        })

    fm = (site_entry or {}).get('fixed_mapping') or {}
    for pname, triplets in (fm.get('port_to_ocs_triplets') or {}).items():
        if pname not in seen_names:
            seen_names.add(pname)
            port_names.append(pname)
        details.append({
            'name': pname,
            'role': 'ocs',
            'ocs_triplets': triplets,
            'serial': '',
        })

    if not port_names:
        port_names = [f'port_{i}' for i in range(1, 33)]

    return port_names, details


def enrich_topology_ports_from_cache(
    topo: LabTopology,
    site_data: Optional[Dict] = None,
    *,
    refresh: bool = False,
) -> int:
    """Update nodes with port lists, port_details, and slot_layout."""
    from .topology_device_ports import (
        build_node_slot_layout,
        fetch_triplet_to_xcon_for_topo,
        load_site_port_data,
        persist_layout_on_node,
    )

    site_data = site_data or {}
    site_by_ip = {
        (ch.get('ip') or ch.get('ip_address', '')): ch
        for ch in site_data.get('keysight_chassis') or []
    }
    site_port_data = load_site_port_data()
    triplet_to_xcon = fetch_triplet_to_xcon_for_topo(topo) if refresh else {}
    updated = 0
    for node in topo.nodes.select_related('device').all():
        if node.node_type == 'chassis':
            extra = dict(node.extra or {})
            cid = extra.get('chassis_id')
            ip = extra.get('device_ip') or ''
            if not cid and ip:
                ch = KeysightChassis.objects.filter(ip_address=ip).first()
                if ch:
                    cid = ch.pk
                    extra['chassis_id'] = cid
            if not cid:
                continue
            ch = KeysightChassis.objects.filter(pk=cid).first()
            if not ch:
                continue
            site_entry = site_by_ip.get(ch.ip_address) or site_by_ip.get(ip)
            names, details = ports_from_chassis_and_site(ch, site_entry, refresh=refresh)
            extra['ports'] = names
            extra['port_details'] = details
            node.extra = extra
            node.save(update_fields=['extra'])
            layout = build_node_slot_layout(
                node, topo, site_port_data=site_port_data,
                triplet_to_xcon=triplet_to_xcon, refresh=refresh,
            )
            if layout.get('slots'):
                persist_layout_on_node(node, layout)
            updated += 1
            continue

        if node.node_type in ('ocs', 'switch', 'firewall', 'server'):
            layout = build_node_slot_layout(
                node, topo, site_port_data=site_port_data,
                triplet_to_xcon=triplet_to_xcon, refresh=refresh,
            )
            if layout.get('slots'):
                persist_layout_on_node(node, layout)
                updated += 1
    return updated


def apply_lldp_links_to_topology(topo: LabTopology) -> int:
    """Add DAC links from global cached topology for nodes in this lab topology."""
    from .topology import get_cached_topology, CHASSIS_NODE_PREFIX

    try:
        lldp_topo = get_cached_topology() or {}
    except Exception:
        return 0

    ip_to_node: Dict[str, LabTopologyNode] = {}
    devid_to_node: Dict[int, LabTopologyNode] = {}
    chassisid_to_node: Dict[int, LabTopologyNode] = {}
    for n in topo.nodes.select_related('device').all():
        if n.device_id and n.device:
            devid_to_node[int(n.device_id)] = n
            if n.device.ip_address:
                ip_to_node[n.device.ip_address] = n
        cid = (n.extra or {}).get('chassis_id')
        if cid:
            chassisid_to_node[int(cid)] = n
        ip = (n.extra or {}).get('device_ip', '')
        if ip:
            ip_to_node[ip] = n

    def _map_gid(gid: str) -> Optional[LabTopologyNode]:
        if gid.startswith(CHASSIS_NODE_PREFIX):
            try:
                ch_id = int(gid[len(CHASSIS_NODE_PREFIX):])
            except ValueError:
                return None
            return chassisid_to_node.get(ch_id)
        try:
            did = int(gid)
        except (TypeError, ValueError):
            return None
        node = devid_to_node.get(did)
        if node:
            return node
        dev = Device.objects.filter(pk=did).first()
        if dev and dev.ip_address:
            return ip_to_node.get(dev.ip_address)
        return None

    created = 0
    for e in lldp_topo.get('links') or []:
        if e.get('status') not in ('up', 'active', True):
            continue
        na = _map_gid(str(e.get('source', '')))
        nb = _map_gid(str(e.get('target', '')))
        if not na or not nb or na.pk == nb.pk:
            continue
        if _link_exists(topo, na.pk, '', nb.pk, ''):
            continue
        LabTopologyLink.objects.create(
            topology=topo,
            node_a=na,
            port_a='',
            node_b=nb,
            port_b='',
            cable_type='dac',
            color='#f59e0b',
            label='LLDP',
            extra={'discovery': 'lldp_cache'},
        )
        created += 1
    if created:
        topo.save(update_fields=['updated_at'])
    return created
