"""
Unified slot/port layout for Lab Topology Designer and Port Fabric.

Produces the same grouping for chassis (RG/slot), OCS (shelf/bank×8),
switches (OCS uplink vs DAC ranges), and servers.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import Device, KeysightChassis, LabTopology, LabTopologyNode

HEALTH_COLOR = {
    'active': '#22c55e',
    'alarm': '#ef4444',
    'lldp_only': '#f59e0b',
    'planned': '#3b82f6',
    'dac': '#8b5cf6',
    'down': '#dc2626',
    'unused': '#1e293b',
    'unknown': '#334155',
}


def _portnum(label: str) -> int:
    m = re.search(r'(\d+)$', str(label or ''))
    return int(m.group(1)) if m else 0


def _health_led(health: str) -> str:
    return {
        'active': 'green', 'alarm': 'red', 'lldp_only': 'amber',
        'planned': 'yellow', 'dac': 'green', 'down': 'red', 'unused': 'off',
    }.get(health or 'unused', 'off')


def ixos_link_is_down(link_state: str = '', led_color: str = '') -> bool:
    """True when IxOS / CMC reports link down or loss of signal.

    Idle/unprovisioned ports (unused link_state, off LED) are NOT down — only
    explicit down telemetry or red/alarm LED with a real link_state counts.
    """
    ls = (link_state or '').lower().replace(' ', '').replace('_', '')
    if not ls or ls in ('unused', 'idle', 'unknown', 'na', 'n/a', 'empty'):
        return False
    if ls in (
        'down', 'notconnect', 'notconnected', 'oos', 'offline', 'disabled',
        'lossofsignal', 'nolink', 'linkdown', 'fault', 'error',
    ):
        return True
    if 'loss' in ls and 'signal' in ls:
        return True
    led = (led_color or '').lower()
    if led in ('red', 'alarm') and ls not in ('up', 'connected', 'linkup'):
        return True
    return False


def fabric_oper_state(
    *,
    health: str = 'unused',
    link_up: bool = False,
    link_state: str = '',
    role: str = 'dac',
) -> str:
    """
    Operational LED for port fabric / topology views.

    Green = up, orange = administratively down, red = down, dark = idle.
    Physical IxOS link_state is checked before abstract health flags.
    """
    ls = (link_state or '').lower()
    if ls in ('admin_down', 'administratively down', 'disabled', 'shutdown'):
        return 'admin_down'
    if ixos_link_is_down(link_state, ''):
        return 'down'
    if health in ('alarm', 'down'):
        return 'down'
    if ls in ('down', 'notconnect', 'oos'):
        return 'down'
    if link_up or health in ('active', 'lldp_only'):
        return 'up'
    if health in ('dac', 'planned') and ls in ('up', 'connected', 'linkup'):
        return 'up'
    return 'idle'


def fabric_state_from_ixos_port(
    p: Dict[str, Any],
    *,
    role: str = 'dac',
    live: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, bool, str]:
    """
    Derive (health, oper_state, link_up, link_type) from IxOS port row + optional live overlay.
    """
    live = live or {}
    ls = str(p.get('link_state') or live.get('link_state') or '')
    led = str(p.get('led_color') or live.get('led_color') or '')
    role = live.get('role') or p.get('role') or role

    pre_health = (live.get('health') or p.get('health') or '').strip()
    if pre_health in ('active', 'lldp_only', 'dac', 'planned', 'alarm'):
        link_up = bool(live.get('link_up', p.get('link_up', pre_health != 'alarm')))
        if pre_health == 'alarm' and not link_up:
            return 'alarm', 'down', False, fabric_link_type(health='alarm', role=role)
        oper = live.get('oper_state') or fabric_oper_state(
            health=pre_health, link_up=link_up, link_state=ls, role=role,
        )
        return pre_health, oper, link_up, fabric_link_type(health=pre_health, role=role)

    if ixos_link_is_down(ls, led):
        return 'down', 'down', False, fabric_link_type(health='down', role=role)

    ls_l = ls.lower()
    if ls_l in ('up', 'connected', 'linkup'):
        health = 'active' if role == 'ocs' else 'lldp_only'
        oper = fabric_oper_state(health=health, link_up=True, link_state=ls, role=role)
        return health, oper, True, fabric_link_type(health=health, role=role)

    if (p.get('owner') or live.get('owner') or '').strip().lower() not in ('', 'free'):
        health = 'dac'
        oper = fabric_oper_state(health=health, link_state=ls, role=role)
        return health, oper, oper == 'up', fabric_link_type(health=health, role=role)

    oper = fabric_oper_state(health='unused', link_state=ls, role=role)
    return 'unused', oper, False, fabric_link_type(health='unused', role=role)


def fabric_link_type(*, health: str, role: str) -> str:
    """Box fill: dac (purple), lldp (orange), ocs (brown), planned (grey)."""
    if health == 'lldp_only':
        return 'lldp'
    if role == 'ocs' or health in ('active', 'alarm'):
        return 'ocs'
    if health in ('dac', 'planned') or role == 'dac':
        return 'dac'
    if health == 'planned':
        return 'planned'
    return role or 'unused'


def _designer_port(
    name: str,
    *,
    port_number: int | None = None,
    port_display: str = '',
    role: str = 'dac',
    health: str = 'unused',
    link_state: str = '',
    serial: str = '',
    ocs_triplet: str = '',
    extra: Optional[Dict] = None,
) -> Dict[str, Any]:
    p = {
        'name': name,
        'port_number': port_number if port_number is not None else _portnum(name),
        'port_display': port_display or str(port_number or _portnum(name) or name),
        'role': role,
        'health': health,
        'link_state': link_state or health,
        'led_color': _health_led(health),
        'serial': serial,
        'ocs_triplet': ocs_triplet,
    }
    if extra:
        p.update(extra)
    return p


def _rows_of_eight(ports: List[Dict], label_prefix: str) -> List[Dict]:
    """Split ports into resource_groups of up to 8 per row (OCS bank style)."""
    rgs = []
    for i in range(0, len(ports), 8):
        chunk = ports[i:i + 8]
        rgs.append({
            'number': (i // 8) + 1,
            'label': f'{label_prefix} {chunk[0].get("port_display", "")}–{chunk[-1].get("port_display", "")}',
            'title': label_prefix,
            'ports': chunk,
        })
    return rgs


def build_ocs_slot_layout(
    *,
    triplet_to_xcon: Dict[str, Dict],
    site_triplets: Set[str],
    all_triplets: Optional[List[str]] = None,
    physical_ports: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Shelf → module (bank) → 8 port cells, matching OCS device page."""
    from . import ocs_helpers

    ports_for_grid: List[Dict[str, Any]] = []
    seen_t: Set[str] = set()

    for row in physical_ports or []:
        key = ocs_helpers.norm_ocs_triplet_key(
            str(row.get('name') or row.get('display_name') or ''),
        )
        if not key or key in seen_t:
            continue
        seen_t.add(key)
        conn = str(row.get('ocs_conn') or row.get('ocs_connid') or '').strip()
        active = bool(conn) or (row.get('status') or '').lower() in ('connected', 'up')
        ports_for_grid.append({
            'ocs_triplet_key': key,
            'name': key,
            'status_color': 'green' if active else 'off',
            'health': 'active' if active else 'unused',
            'ocs_conn': conn,
        })

    for t, xc in triplet_to_xcon.items():
        if t in seen_t:
            continue
        seen_t.add(t)
        # Patched triplet = green on device page (alarm details in tooltip only)
        health = 'active'
        ports_for_grid.append({
            'ocs_triplet_key': t,
            'name': t,
            'status_color': 'green',
            'health': health,
            'loss_db': xc.get('loss_db'),
            'path_id': xc.get('path_id', ''),
        })

    for t in (all_triplets or []):
        if t not in seen_t:
            ports_for_grid.append({
                'ocs_triplet_key': t,
                'name': t,
                'status_color': 'off',
                'health': 'unused',
                'is_ocs_placeholder': True,
            })

    shelves = ocs_helpers._group_ocs_shelves_eight(ports_for_grid, site_triplets)
    slots_out: List[Dict[str, Any]] = []
    flat: List[Dict[str, Any]] = []

    for shelf in shelves:
        slot_entry: Dict[str, Any] = {
            'slot': shelf.get('shelf_id', 0),
            'label': shelf.get('label', f"Shelf {shelf.get('shelf_id', '')}"),
            'card_type': f"{shelf.get('bank_count', 0)} banks",
            'is_mgmt': False,
            'layout': 'ocs_shelf',
            'resource_groups': [],
            'ports': [],
        }
        for mod in shelf.get('modules') or []:
            rg_ports = []
            for cell in mod.get('ports') or []:
                if not cell:
                    continue
                key = cell.get('ocs_triplet_key') or cell.get('name', '')
                health = cell.get('health') or (
                    'active' if cell.get('status_color') == 'green' else
                    'alarm' if cell.get('status_color') == 'red' else 'unused'
                )
                pd = _designer_port(
                    key,
                    port_display=cell.get('port_digit') or key.split('.')[-1] if key else '?',
                    role='ocs',
                    health=health,
                    ocs_triplet=key,
                    extra={'ocs_bank_label': mod.get('label', '')},
                )
                rg_ports.append(pd)
                flat.append(pd)
            slot_entry['resource_groups'].append({
                'number': mod.get('module_id'),
                'label': mod.get('label', ''),
                'title': f"Bank {mod.get('label', '')}",
                'layout': 'ocs_bank',
                'ports': rg_ports,
            })
            slot_entry['ports'].extend(rg_ports)
        slots_out.append(slot_entry)

    return {'slots': slots_out, 'flat_ports': flat, 'source': 'ocs_live'}


def iter_switch_site_ports(site_ports: Dict[str, Dict]):
    """
    Yield (port_key, site_entry) for switch layout.

    OCS logical ports with two site triplets expand to one cell per fiber (fanout leg),
    keyed by triplet (e.g. 2.1.1) with logical_port=port_17 preserved.
    """
    for pl in sorted(site_ports.keys(), key=_portnum):
        sp = site_ports[pl]
        role = sp.get('role', 'ocs')
        triplets = [str(t) for t in (sp.get('triplets') or []) if t]
        if role == 'ocs' and len(triplets) > 1:
            for i, t in enumerate(triplets):
                leg_display = t.split('.')[-1] if '.' in t else str(i + 1)
                yield t, {
                    **sp,
                    'triplets': [t],
                    'logical_port': pl,
                    'fanout_index': i,
                    'port_display': leg_display,
                }
        else:
            yield pl, sp


def enrich_chassis_slot_layout_with_ocs(
    slots: List[Dict[str, Any]],
    site_ports: Dict[str, Dict],
    triplet_to_xcon: Optional[Dict[str, Dict]] = None,
) -> List[Dict[str, Any]]:
    """Map each AresONE RG leg (1.1, 1.2, …) to its OCS triplet and live patch health."""
    if not site_ports:
        return slots
    out: List[Dict[str, Any]] = []
    for slot in slots or []:
        slot = dict(slot)
        rgs_out: List[Dict[str, Any]] = []
        flat_ports: List[Dict[str, Any]] = []
        for rg in slot.get('resource_groups') or []:
            rg = dict(rg)
            rg_num = rg.get('number')
            try:
                rg_num = int(rg_num)
            except (TypeError, ValueError):
                rg_num = None
            logical = f'port_{rg_num}' if rg_num else ''
            trips = [str(t) for t in ((site_ports.get(logical) or {}).get('triplets') or [])]
            ports_out: List[Dict[str, Any]] = []
            for idx, p in enumerate(rg.get('ports') or []):
                p = dict(p)
                if idx < len(trips):
                    t = trips[idx]
                    p['ocs_triplet'] = t
                    p['logical_port'] = logical
                    p['role'] = 'ocs'
                    xc = (triplet_to_xcon or {}).get(t) if triplet_to_xcon else None
                    if xc:
                        health = xc.get('health', 'active')
                        p['health'] = health
                        if health == 'active' and p.get('link_state') not in ('down',):
                            p['link_state'] = p.get('link_state') or 'up'
                    elif (p.get('link_state') or '').lower() == 'up':
                        p['health'] = 'active'
                ports_out.append(p)
            rg['ports'] = ports_out
            rgs_out.append(rg)
            flat_ports.extend(ports_out)
        if rgs_out:
            slot['resource_groups'] = rgs_out
            slot['ports'] = flat_ports
        out.append(slot)
    return out


def build_switch_slot_layout(
    site_ports: Dict[str, Dict],
    extra_ports: List[str],
    *,
    node_label: str = '',
    triplet_to_xcon: Optional[Dict[str, Dict]] = None,
) -> Dict[str, Any]:
    """Group switch ports: OCS uplink block (fanout per triplet), then DAC blocks in rows of 8."""
    ocs_list: List[Dict] = []
    dac_list: List[Dict] = []

    for pl, sp in iter_switch_site_ports(site_ports):
        role = sp.get('role', 'ocs')
        logical = sp.get('logical_port') or pl
        pn = _portnum(logical)
        trips = sp.get('triplets') or []
        t0 = str(trips[0]) if trips else ''
        health = 'unused'
        if t0 and triplet_to_xcon and t0 in triplet_to_xcon:
            health = triplet_to_xcon[t0].get('health', 'active')
        pd = _designer_port(
            pl,
            port_number=pn,
            port_display=sp.get('port_display') or (t0.split('.')[-1] if t0 else str(pn or pl)),
            role=role,
            health=health,
            ocs_triplet=t0,
            extra={
                'logical_port': logical,
                'fanout_index': sp.get('fanout_index'),
            } if sp.get('logical_port') else None,
        )
        if role == 'ocs':
            ocs_list.append(pd)
        else:
            dac_list.append(pd)

    for pl in sorted(set(extra_ports) - set(site_ports.keys()), key=_portnum):
        if pl in site_ports:
            continue
        pn = _portnum(pl)
        dac_list.append(_designer_port(pl, port_number=pn, role='dac', health='unused'))

    slots_out: List[Dict] = []
    flat: List[Dict] = []

    if ocs_list:
        rgs = _rows_of_eight(ocs_list, 'OCS')
        slots_out.append({
            'slot': 0,
            'label': 'OCS Uplink',
            'card_type': 'Spine→OCS',
            'layout': 'switch_ocs',
            'resource_groups': rgs,
            'ports': ocs_list,
        })
        flat.extend(ocs_list)

    if dac_list:
        # Split DAC into 49-64 and 17-48 style bands when present
        hi = [p for p in dac_list if (p.get('port_number') or 0) >= 49]
        mid = [p for p in dac_list if 17 <= (p.get('port_number') or 0) < 49]
        lo = [p for p in dac_list if (p.get('port_number') or 0) < 17 and p not in hi and p not in mid]
        bands = []
        if hi:
            bands.append(('DAC 49–64', hi))
        if mid:
            bands.append(('DAC 17–48', mid))
        if lo:
            bands.append(('DAC / Eth', lo))
        if not bands:
            bands.append(('DAC Ports', dac_list))

        slot_entry: Dict[str, Any] = {
            'slot': 1,
            'label': 'Fabric / DAC',
            'card_type': node_label,
            'layout': 'switch_dac',
            'resource_groups': [],
            'ports': [],
        }
        for band_label, band_ports in bands:
            for rg in _rows_of_eight(band_ports, band_label):
                slot_entry['resource_groups'].append(rg)
            slot_entry['ports'].extend(band_ports)
            flat.extend(band_ports)
        slots_out.append(slot_entry)

    if not slots_out and dac_list:
        slots_out.append({
            'slot': 0, 'label': 'Ports', 'resource_groups': _rows_of_eight(dac_list, 'Ports'),
            'ports': dac_list, 'layout': 'switch_flat',
        })

    return {'slots': slots_out, 'flat_ports': flat, 'source': 'switch_site'}


def build_server_slot_layout(port_names: List[str], *, label: str = 'NICs') -> Dict[str, Any]:
    ports = [_designer_port(p, role='optic', health='unused') for p in sorted(port_names, key=_portnum)]
    slot = {
        'slot': 0,
        'label': label,
        'card_type': '',
        'layout': 'server',
        'resource_groups': _rows_of_eight(ports, label) if len(ports) > 8 else [{
            'number': 1, 'label': label, 'ports': ports,
        }],
        'ports': ports,
    }
    return {'slots': [slot], 'flat_ports': ports, 'source': 'server'}


def build_node_slot_layout(
    node: LabTopologyNode,
    topo: LabTopology,
    *,
    site_port_data: Optional[Dict[str, Dict]] = None,
    triplet_to_xcon: Optional[Dict] = None,
    refresh: bool = False,
    ocs_physical_ports: Optional[List[Dict[str, Any]]] = None,
    additional_switch_ports: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build slot_layout for any topology node type."""
    from .topology_chassis_ports import (
        build_slot_layout,
        layout_from_port_details,
        merge_layout_with_saved,
    )

    extra = dict(node.extra or {})
    node_id = node.pk
    ip = ''
    if node.device:
        ip = node.device.ip_address or ''
    ip = ip or extra.get('device_ip', '') or extra.get('mgmt_ipv4', '')

    site_ports = (site_port_data or {}).get(ip, {})
    saved_layout = extra.get('slot_layout')

    if node.node_type == 'chassis':
        from .keysight_views import _get_cached, fetch_chassis_data
        from .lab_topology_views import _chassis_name_to_ip_helper

        cid = extra.get('chassis_id')
        ch = KeysightChassis.objects.filter(pk=cid).first() if cid else None
        if not ch and ip:
            ch = KeysightChassis.objects.filter(ip_address=ip).first()
        if not ch:
            ip2 = _chassis_name_to_ip_helper(node.label)
            if ip2:
                ch = KeysightChassis.objects.filter(ip_address=ip2).first()
        if ch:
            ip = ip or ch.ip_address or ''
            site_ports = (site_port_data or {}).get(ip, {}) if ip else site_ports
            cached = {}
            if refresh:
                try:
                    from .keysight_views import fetch_chassis_data
                    cached = fetch_chassis_data(ch) or {}
                except Exception:
                    pass
            if not cached:
                cached = _get_cached(ch.id) or {}
            # Never SSH on cache miss unless caller explicitly passed refresh=True
            # (port-fabric initial load must stay fast).
            layout = build_slot_layout(ch, cached) if cached.get('ports') or cached.get('cards') else {}
            layout = merge_layout_with_saved(layout, saved_layout)
            if site_ports:
                layout['slots'] = enrich_chassis_slot_layout_with_ocs(
                    layout.get('slots') or [],
                    site_ports,
                    triplet_to_xcon or {},
                )
                link_index = build_ixos_link_index(ch, refresh=refresh)
                if link_index:
                    layout['slots'] = merge_ixos_link_state_into_slots(
                        layout['slots'], link_index,
                    )
            layout['chassis_id'] = ch.pk
            return layout

        layout = layout_from_port_details(
            extra.get('port_details') or [],
            chassis_type=extra.get('chassis_type', ''),
            port_names=list(extra.get('ports') or []),
        )
        layout['source'] = layout.get('source') or 'stored_chassis'
        return layout

    if node.node_type == 'ocs':
        site_triplets: Set[str] = set()
        all_triplets: List[str] = []
        for sw_ports in (site_port_data or {}).values():
            for sp in sw_ports.values():
                for t in sp.get('triplets') or []:
                    site_triplets.add(str(t))
                    all_triplets.append(str(t))
        physical_ports = list(
            ocs_physical_ports or extra.get('ocs_physical_ports') or [],
        )
        layout = build_ocs_slot_layout(
            triplet_to_xcon=triplet_to_xcon or {},
            site_triplets=site_triplets,
            all_triplets=all_triplets,
            physical_ports=physical_ports,
        )
        if not layout.get('slots') and saved_layout:
            return {'slots': saved_layout, 'flat_ports': _flat_from_slots(saved_layout), 'source': 'saved_layout'}
        return layout

    if node.node_type in ('switch', 'firewall'):
        extra_ports = []
        for ep in extra.get('ports') or []:
            extra_ports.append(ep if isinstance(ep, str) else ep.get('name', ''))
        for ep in additional_switch_ports or []:
            if ep and ep not in extra_ports and ep not in site_ports:
                extra_ports.append(ep)
        layout = build_switch_slot_layout(
            site_ports,
            extra_ports,
            node_label=node.label,
            triplet_to_xcon=triplet_to_xcon or {},
        )
        if not layout.get('slots') and saved_layout:
            return {'slots': saved_layout, 'flat_ports': _flat_from_slots(saved_layout), 'source': 'saved_layout'}
        return layout

    if node.node_type == 'server':
        ports = [p if isinstance(p, str) else p.get('name', '') for p in (extra.get('ports') or [])]
        layout = build_server_slot_layout(ports, label=node.label)
        if saved_layout:
            return {'slots': saved_layout, 'flat_ports': _flat_from_slots(saved_layout), 'source': 'saved_layout'}
        return layout

    # generic
    ports = [p if isinstance(p, str) else p.get('name', '') for p in (extra.get('ports') or [])]
    if saved_layout:
        return {'slots': saved_layout, 'flat_ports': _flat_from_slots(saved_layout), 'source': 'saved_layout'}
    return build_server_slot_layout(ports, label=node.label or 'Ports')


def _flat_from_slots(slots: List[Dict]) -> List[Dict]:
    flat = []
    seen: Set[str] = set()
    for sl in slots or []:
        for p in sl.get('ports') or []:
            n = p.get('name')
            if n and n not in seen:
                seen.add(n)
                flat.append(p)
    return flat


def slot_layout_to_port_groups(
    node_id: str,
    slots: List[Dict],
    health_by_name: Optional[Dict[str, Dict]] = None,
    health_by_index: Optional[Dict[int, Dict]] = None,
) -> List[Dict[str, Any]]:
    """
    Convert designer slot_layout → Port Fabric port_groups[].
    health_by_name: port label → fabric port entry dict (from live enrich pass).
    """
    node_key = f'node_{node_id}' if not str(node_id).startswith('node_') else str(node_id)
    if node_key.startswith('node_node_'):
        node_key = node_key.replace('node_node_', 'node_', 1)

    groups_out: List[Dict[str, Any]] = []

    for slot in slots or []:
        slot_lbl = slot.get('label', '') or f"Slot {slot.get('slot', '')}"
        slot_num = slot.get('slot') or slot.get('card_number')
        slot_card_type = slot.get('card_type', '') or ''
        layout = slot.get('layout', '')
        if layout == 'ocs_shelf':
            for rg in slot.get('resource_groups') or []:
                fabric_ports = []
                for p in rg.get('ports') or []:
                    if slot_num is not None and p.get('card_number') is None:
                        p = {**p, 'card_number': slot_num}
                    fabric_ports.append(_fabric_port_from_designer(
                        node_key, p, health_by_name, health_by_index,
                        slot_label=slot_lbl, slot_card_type=slot_card_type,
                    ))
                groups_out.append({
                    'label': f"{slot_lbl} · {rg.get('label', '')}",
                    'role': 'ocs',
                    'card_type': slot_card_type,
                    'slot': slot_num,
                    'ports': fabric_ports,
                })
        else:
            for rg in slot.get('resource_groups') or []:
                fabric_ports = []
                for p in rg.get('ports') or []:
                    if slot_num is not None and p.get('card_number') is None:
                        p = {**p, 'card_number': slot_num}
                    fabric_ports.append(_fabric_port_from_designer(
                        node_key, p, health_by_name, health_by_index,
                        slot_label=slot_lbl, slot_card_type=slot_card_type,
                    ))
                if not fabric_ports:
                    continue
                role = 'dac'
                if any((x.get('role') == 'ocs') for x in (rg.get('ports') or [])):
                    role = 'ocs'
                groups_out.append({
                    'label': f"{slot_lbl} · {rg.get('label') or rg.get('title') or 'Ports'}",
                    'role': role,
                    'card_type': slot_card_type,
                    'slot': slot_num,
                    'ports': fabric_ports,
                })
            if not slot.get('resource_groups') and slot.get('ports'):
                fabric_ports = []
                for p in slot['ports']:
                    if slot_num is not None and p.get('card_number') is None:
                        p = {**p, 'card_number': slot_num}
                    fabric_ports.append(_fabric_port_from_designer(
                        node_key, p, health_by_name, health_by_index,
                        slot_label=slot_lbl, slot_card_type=slot_card_type,
                    ))
                role = 'dac'
                if any((x.get('role') == 'ocs') for x in slot['ports']):
                    role = 'ocs'
                groups_out.append({
                    'label': slot_lbl,
                    'role': role,
                    'card_type': slot_card_type,
                    'slot': slot_num,
                    'ports': fabric_ports,
                })

    return groups_out


def fanout_port_label_for_triplet(
    triplet: str,
    *,
    node_type: str,
    site_ports: Dict[str, Dict],
) -> str:
    """Map OCS triplet to designer / fabric port label (RG leg or switch triplet key)."""
    from . import ocs_helpers

    t = ocs_helpers.norm_ocs_triplet_key(triplet)
    if node_type == 'switch':
        return t
    if node_type == 'chassis':
        for pl, sp in site_ports.items():
            trips = [
                ocs_helpers.norm_ocs_triplet_key(str(x))
                for x in (sp.get('triplets') or [])
            ]
            if t in trips:
                idx = trips.index(t)
                rg = _portnum(pl)
                return f'{rg}.{idx + 1}'
    return t


def fanout_peer_port_label(
    peer_triplet: str,
    peer_ip: str,
    *,
    site_port_data: Dict[str, Dict[str, Dict]],
    peer_node_type: str = '',
) -> str:
    """Resolve peer fanout label for connections (not flat port_N)."""
    from . import ocs_helpers

    peer_site = site_port_data.get(peer_ip, {})
    t = ocs_helpers.norm_ocs_triplet_key(peer_triplet)
    if peer_node_type == 'switch':
        return t
    if peer_node_type == 'chassis':
        return fanout_port_label_for_triplet(t, node_type='chassis', site_ports=peer_site)
    lbl = fanout_port_label_for_triplet(t, node_type='chassis', site_ports=peer_site)
    if lbl != t:
        return lbl
    if t in peer_site:
        return t
    return fanout_port_label_for_triplet(t, node_type='switch', site_ports=peer_site)


def iter_ocs_fanout_site_ports(
    site_ports: Dict[str, Dict],
    node_type: str,
):
    """
    Yield (fabric_label, site_entry, logical_port) for each OCS fanout leg.

    Switch: one row per triplet (2.1.1). Chassis: RG legs (1.1, 1.2) when multi-fiber.
    """
    if node_type == 'switch':
        for pl, sp in iter_switch_site_ports(site_ports):
            if sp.get('role', 'ocs') != 'ocs':
                continue
            logical = sp.get('logical_port') or pl
            yield pl, sp, logical
        return
    if node_type == 'chassis':
        for pl in sorted(site_ports.keys(), key=_portnum):
            sp = site_ports[pl]
            if sp.get('role', 'ocs') != 'ocs':
                continue
            trips = [str(t) for t in (sp.get('triplets') or []) if t]
            if len(trips) > 1:
                rg = _portnum(pl)
                for idx, t in enumerate(trips):
                    leg = f'{rg}.{idx + 1}'
                    yield leg, {
                        **sp,
                        'triplets': [t],
                        'logical_port': pl,
                        'fanout_index': idx,
                    }, pl
            else:
                yield pl, sp, pl
        return
    for pl in sorted(site_ports.keys(), key=_portnum):
        sp = site_ports[pl]
        if sp.get('role', 'ocs') == 'ocs':
            yield pl, sp, pl


def slots_have_ocs_fanout_legs(slots: List[Dict]) -> bool:
    """True when slot_layout already uses per-fiber RG legs (1.1) with OCS triplets."""
    for slot in slots or []:
        for rg in slot.get('resource_groups') or []:
            for p in rg.get('ports') or []:
                name = str(p.get('name') or '')
                if p.get('ocs_triplet') and re.match(r'^\d+\.\d+$', name):
                    return True
    return False


def apply_ocs_live_to_port_groups(
    port_groups: List[Dict[str, Any]],
    triplet_to_xcon: Dict[str, Dict],
    triplet_map: Dict,
    ip_to_node_id: Dict[str, str],
    site_port_data: Dict[str, Dict[str, Dict]],
    node_ip: str,
    node_id: str,
    *,
    ip_to_node_type: Optional[Dict[str, str]] = None,
) -> None:
    """Overlay live OCS cross-connect state on designer-aligned port_groups."""
    from . import ocs_helpers

    for grp in port_groups or []:
        for p in grp.get('ports') or []:
            raw_t = p.get('ocs_triplet') or ''
            lbl = str(p.get('label') or p.get('name') or '')
            if not raw_t and re.match(r'^\d+\.\d+\.\d+$', lbl):
                raw_t = lbl
            t = ocs_helpers.norm_ocs_triplet_key(str(raw_t))
            if not t:
                continue
            xc = triplet_to_xcon.get(t)
            if not xc:
                continue
            health = xc.get('health', 'active')
            link_up = health == 'active'
            p['health'] = health
            p['link_up'] = link_up
            p['health_color'] = HEALTH_COLOR.get(health, HEALTH_COLOR['unused'])
            p['ocs_triplet'] = t
            p['ocs_path_id'] = xc.get('path_id', '')
            p['loss_db'] = xc.get('loss_db')
            p['role'] = p.get('role') or 'ocs'
            p['link_type'] = fabric_link_type(health=health, role='ocs')
            p['oper_state'] = fabric_oper_state(
                health=health,
                link_up=link_up,
                link_state=p.get('link_state', ''),
                role='ocs',
            )
            peer_t = ocs_helpers.norm_ocs_triplet_key(xc.get('peer', ''))
            peer_info = triplet_map.get(peer_t, {})
            peer_ip = (peer_info.get('ip') or '').strip()
            if peer_ip:
                peer_nid = ip_to_node_id.get(peer_ip, '')
                peer_nt = (ip_to_node_type or {}).get(peer_ip, '')
                peer_lbl = fanout_peer_port_label(
                    peer_t,
                    peer_ip,
                    site_port_data=site_port_data,
                    peer_node_type=peer_nt,
                )
                if peer_nid:
                    p['peer_device_id'] = peer_nid
                    p['peer_port_label'] = peer_lbl
                    p['peer_port_id'] = f'{peer_nid}__{peer_lbl}'


def build_fabric_health_lookup(
    ocs_ports: List[Dict],
    dac_ports: List[Dict],
    port_details: Optional[List[Dict]] = None,
) -> Tuple[Dict[str, Dict], Dict[int, Dict]]:
    """Index live fabric entries by port label, triplet, and numeric index."""
    by_name: Dict[str, Dict] = {}
    by_index: Dict[int, Dict] = {}
    for e in ocs_ports + dac_ports:
        lbl = e.get('label', '')
        if lbl:
            by_name[lbl] = e
            import re as _re_lbl
            m = _re_lbl.search(r'(?:^port[_\s]*)?(\d+)$', lbl, _re_lbl.I)
            if m:
                by_name[f'Ethernet{m.group(1)}'] = e
        trip = e.get('ocs_triplet')
        if trip:
            by_name[str(trip)] = e
        idx = e.get('index')
        if idx is not None:
            by_index[int(idx)] = e
    for pd in port_details or []:
        name = pd.get('name', '')
        if not name:
            continue
        # Bare "1" collides across slots — only index canonical names.
        if re.fullmatch(r'\d+', str(name)) and not pd.get('card_number'):
            continue
        cn = pd.get('card_number')
        pn = pd.get('port_number') if pd.get('port_number') is not None else _portnum(name)
        ls = pd.get('link_state') or ''
        led = pd.get('led_color') or ''
        role = pd.get('role', 'dac')
        health, oper, link_up, _lt = fabric_state_from_ixos_port(
            {'link_state': ls, 'led_color': led, 'role': role},
            role=role,
        )
        entry = {
            'label': name,
            'health': health,
            'link_up': link_up,
            'link_state': ls,
            'led_color': led,
            'oper_state': oper,
            'role': role,
            'serial': pd.get('serial', ''),
        }
        by_name[name] = entry
        if cn is not None and pn is not None:
            by_name[f'c{cn}p{pn}'] = entry
        if pn and re.match(r'^port[_\s]*\d+', str(name), re.I):
            by_index[int(pn)] = entry
    return by_name, by_index


def build_ixos_link_index(chassis: KeysightChassis, *, refresh: bool = False) -> Dict[str, Dict]:
    """Live IxOS port rows keyed by c{card}p{num}, port_N, and display label."""
    from .topology_dac_finder import _iter_cached_chassis_ports

    try:
        from .keysight_views import _get_cached, fetch_chassis_data
    except ImportError:
        return {}

    cached: Dict[str, Any] = {}
    if refresh:
        try:
            cached = fetch_chassis_data(chassis) or {}
        except Exception:
            cached = {}
    if not cached:
        cached = _get_cached(chassis.id) or {}

    index: Dict[str, Dict] = {}
    for port in _iter_cached_chassis_ports(cached):
        cn = port.get('card_number')
        pn = port.get('port_number')
        if cn is not None and pn is not None:
            index[f'c{int(cn)}p{int(pn)}'] = port
        disp = port.get('port_display')
        if disp is not None:
            if cn is not None:
                index[f's{int(cn)}__{disp}'] = port
        name = port.get('name') or ''
        if name and re.match(r'^c\d+p\d+$', name, re.I):
            index[name.lower()] = port
    return index


def merge_ixos_link_state_into_slots(
    slots: List[Dict[str, Any]],
    link_index: Dict[str, Dict],
) -> List[Dict[str, Any]]:
    """Overlay live link_state / led_color from Keysight cache onto slot_layout ports."""
    if not link_index:
        return slots

    out: List[Dict[str, Any]] = []
    for slot in slots or []:
        slot = dict(slot)
        sn = slot.get('slot') or slot.get('card_number')

        def _merge_port(p: Dict[str, Any]) -> Dict[str, Any]:
            p = dict(p)
            cn = p.get('card_number') or sn
            pn = p.get('port_number')
            live = None
            if cn is not None and pn is not None:
                live = link_index.get(f'c{int(cn)}p{int(pn)}')
            if not live and cn is not None and p.get('port_display') is not None:
                live = link_index.get(f's{int(cn)}__{p.get("port_display")}')
            if live:
                p['link_state'] = live.get('link_state', p.get('link_state'))
                p['led_color'] = live.get('led_color', p.get('led_color'))
                p['owner'] = live.get('owner', p.get('owner', ''))
                p['serial'] = (live.get('transceiver_serial') or p.get('serial') or '').strip()
            return p

        if slot.get('resource_groups'):
            rgs = []
            for rg in slot['resource_groups']:
                rg = dict(rg)
                rg['ports'] = [_merge_port(x) for x in (rg.get('ports') or [])]
                rgs.append(rg)
            slot['resource_groups'] = rgs
        if slot.get('ports'):
            slot['ports'] = [_merge_port(x) for x in slot['ports']]
        out.append(slot)
    return out


def merge_lldp_into_fabric_port_groups(
    port_groups: List[Dict[str, Any]],
    lldp_neighbors: List[Dict[str, Any]],
) -> int:
    """Attach IxOS/AresONE LLDP peers to fabric ports (for Port Fabric panel + B2B).

    Returns count of ports that received at least one neighbor.
    """
    from .topology_lldp import _port_key_variants

    if not lldp_neighbors or not port_groups:
        return 0

    lldp_by_key: Dict[str, List[Dict[str, Any]]] = {}
    for nbr in lldp_neighbors:
        for key in _port_key_variants(nbr.get('local_port', '')):
            lldp_by_key.setdefault(key, []).append(nbr)

    matched_ports = 0
    for grp in port_groups:
        for port in grp.get('ports') or []:
            cn = port.get('slot')
            if cn is None:
                cn = port.get('card_number')
            pn = port.get('index')
            if pn is None:
                pn = port.get('port_number')
            pd = port.get('port_display') or port.get('label') or ''
            candidates: Set[str] = set()
            if cn is not None and pn is not None:
                candidates.update(_port_key_variants(f'{cn}/{pn}'))
                candidates.update(_port_key_variants(f'{cn}.{pn}'))
                candidates.update(_port_key_variants(f'Card {cn} Port {pn}'))
                candidates.update(_port_key_variants(f'c{int(cn)}p{int(pn)}'))
            if pd:
                candidates.update(_port_key_variants(str(pd)))
                candidates.update(_port_key_variants(f'Port {pd}'))
            if port.get('label'):
                candidates.update(_port_key_variants(str(port['label'])))
            if port.get('id'):
                m_id = re.search(r'__s(\d+)__(\d+)$', str(port['id']))
                if m_id:
                    cn_id, pn_id = m_id.group(1), m_id.group(2)
                    candidates.update(_port_key_variants(f'{cn_id}/{pn_id}'))

            hit: Optional[Dict[str, Any]] = None
            for k in candidates:
                rows = lldp_by_key.get(k)
                if rows:
                    hit = rows[0]
                    break
            if not hit:
                continue

            rd = (hit.get('remote_device') or '').strip()
            rp = (hit.get('remote_port') or '').strip()
            mgmt = (hit.get('mgmt_ip') or '').strip()
            if not rd and mgmt:
                rd = mgmt
            port['lldp_remote_device'] = rd
            port['lldp_remote_port'] = rp
            port['lldp_neighbor'] = f'{rd}:{rp}' if rd and rp else (rd or rp)
            port['lldp_mgmt_ip'] = mgmt
            if port.get('health') in ('unused', 'planned', 'dac', 'down'):
                port['health'] = 'lldp_only'
                port['link_up'] = True
                port['link_type'] = 'lldp'
                port['oper_state'] = 'up'
                port['health_color'] = HEALTH_COLOR['lldp_only']
            matched_ports += 1

    return matched_ports


def _fabric_port_id(node_key: str, p: Dict[str, Any], *, slot_label: str = '') -> str:
    """Stable unique port id (avoids duplicate node_X__1 across slots)."""
    name = p.get('name', '') or ''
    slot = p.get('card_number') or p.get('slot')
    if slot is not None and name:
        return f'{node_key}__s{slot}__{name}'
    if slot_label:
        safe = re.sub(r'[^\w.\-]+', '_', slot_label).strip('_')
        return f'{node_key}__{safe}__{name}' if name else f'{node_key}__{safe}'
    return f'{node_key}__{name}' if name else node_key


def _fabric_port_from_designer(
    node_key: str,
    p: Dict[str, Any],
    health_by_name: Optional[Dict[str, Dict]] = None,
    health_by_index: Optional[Dict[int, Dict]] = None,
    *,
    slot_label: str = '',
    slot_card_type: str = '',
) -> Dict[str, Any]:
    name = p.get('name', '')
    pn = p.get('port_number') if p.get('port_number') is not None else _portnum(name)
    live = (
        (health_by_name or {}).get(name)
        or (health_by_index or {}).get(pn)
        or (health_by_name or {}).get(f'c{p.get("card_number")}p{pn}' if p.get('card_number') is not None and pn else '')
        or {}
    )
    role = live.get('role') or p.get('role') or 'dac'
    port_display = p.get('port_display') or str(pn or name)
    merged = dict(p)
    if live:
        merged.update({k: v for k, v in live.items() if k in (
            'link_state', 'led_color', 'owner', 'serial', 'speed', 'health', 'link_up',
        ) and v not in (None, '')})
    health, oper, link_up, link_type = fabric_state_from_ixos_port(merged, role=role, live=live)

    return {
        'id': _fabric_port_id(node_key, p, slot_label=slot_label),
        'label': name,
        'name': name,
        'slot': p.get('card_number') if p.get('card_number') is not None else p.get('slot'),
        'card_number': p.get('card_number') or p.get('slot'),
        'index': pn,
        'port_number': pn,
        'port_display': port_display,
        'slot_group': slot_label,
        'card_type': p.get('card_type') or slot_card_type or '',
        'speed': live.get('speed') or p.get('speed') or ('400G' if role == 'ocs' else '100G'),
        'role': role,
        'link_type': link_type,
        'oper_state': oper,
        'health': health,
        'link_up': link_up,
        'health_color': live.get('health_color') or HEALTH_COLOR.get(health, HEALTH_COLOR['unused']),
        'ocs_triplet': live.get('ocs_triplet') or p.get('ocs_triplet', ''),
        'ocs_path_id': live.get('ocs_path_id', ''),
        'peer_device_id': live.get('peer_device_id', ''),
        'peer_port_id': live.get('peer_port_id', ''),
        'peer_port_label': live.get('peer_port_label', ''),
        'lldp_neighbor': live.get('lldp_neighbor', ''),
        'lldp_remote_device': live.get('lldp_remote_device', ''),
        'lldp_remote_port': live.get('lldp_remote_port', ''),
        'loss_db': live.get('loss_db'),
        'serial': p.get('serial', '') or live.get('serial', ''),
        'link_state': merged.get('link_state', ''),
        'led_color': merged.get('led_color', ''),
    }


def ocs_shelves_from_port_groups(port_groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rebuild shelf → bank columns from flat port_groups (device-page layout)."""
    shelves_map: Dict[str, Dict[str, Any]] = {}
    for pg in port_groups:
        lbl = pg.get('label', '')
        if '·' in lbl:
            shelf_lbl, bank_lbl = [x.strip() for x in lbl.split('·', 1)]
        else:
            shelf_lbl, bank_lbl = lbl, 'Bank'
        if shelf_lbl not in shelves_map:
            shelves_map[shelf_lbl] = {
                'shelf_id': len(shelves_map) + 1,
                'label': shelf_lbl,
                'banks': [],
            }
        shelves_map[shelf_lbl]['banks'].append({
            'label': bank_lbl,
            'ports': list(pg.get('ports') or []),
        })
    return list(shelves_map.values())


def ocs_shelves_from_device_shelves(
    shelves: List[Dict[str, Any]],
    node_key: str,
    health_by_name: Optional[Dict[str, Dict]] = None,
    health_by_index: Optional[Dict[int, Dict]] = None,
) -> List[Dict[str, Any]]:
    """Convert ocs_helpers.build_ocs_shelves() output (modules) → Port Fabric banks."""
    shelves_out: List[Dict[str, Any]] = []
    for shelf in shelves or []:
        banks = []
        for mod in shelf.get('modules') or []:
            ports = []
            for cell in mod.get('ports') or []:
                if not cell:
                    continue
                key = cell.get('ocs_triplet_key') or cell.get('name', '')
                health = cell.get('health') or (
                    'active' if cell.get('status_color') == 'green' else
                    'alarm' if cell.get('status_color') == 'red' else 'unused'
                )
                hp = (health_by_name or {}).get(key) if key else None
                if hp:
                    health = hp.get('health', health)
                ports.append(_fabric_port_from_designer(
                    node_key,
                    {
                        'name': key,
                        'port_display': cell.get('port_digit') or (
                            key.split('.')[-1] if key else '?'
                        ),
                        'health': health,
                        'ocs_triplet': key,
                        'ocs_bank_label': mod.get('label', ''),
                    },
                    health_by_name,
                    health_by_index,
                ))
            banks.append({
                'module_id': mod.get('module_id'),
                'label': mod.get('label', ''),
                'ports': ports,
            })
        shelves_out.append({
            'shelf_id': shelf.get('shelf_id', 0),
            'label': shelf.get('label', f"Shelf {shelf.get('shelf_id', '')}"),
            'bank_count': len(banks),
            'banks': banks,
        })
    return shelves_out


def ocs_shelves_from_slot_layout(
    node_key: str,
    slots: List[Dict[str, Any]],
    health_by_name: Optional[Dict[str, Dict]] = None,
    health_by_index: Optional[Dict[int, Dict]] = None,
) -> List[Dict[str, Any]]:
    """Shelf → horizontal banks → 8 ports (matches OCS device page)."""
    shelves_out: List[Dict[str, Any]] = []
    for slot in slots or []:
        banks = []
        for rg in slot.get('resource_groups') or []:
            ports = [
                _fabric_port_from_designer(node_key, p, health_by_name, health_by_index)
                for p in (rg.get('ports') or [])
            ]
            banks.append({
                'module_id': rg.get('number'),
                'label': rg.get('label', ''),
                'ports': ports,
            })
        shelves_out.append({
            'shelf_id': slot.get('slot', 0),
            'label': slot.get('label', f"Shelf {slot.get('slot', '')}"),
            'bank_count': len(banks),
            'banks': banks,
        })
    return shelves_out


def _chassis_slots_sane(slots: List[Dict[str, Any]], chassis_type: str = '') -> bool:
    """Reject OCS-shelf-shaped layouts mistakenly applied to chassis nodes."""
    if not slots:
        return False
    is_aresone = 'aresone' in (chassis_type or '').lower()
    for slot in slots or []:
        if slot.get('layout') == 'ocs_shelf':
            return False
        for rg in slot.get('resource_groups') or []:
            n = len(rg.get('ports') or [])
            if is_aresone and n > 2:
                return False
    return True


def _is_ixos_chassis_type(chassis_type: str) -> bool:
    return (chassis_type or '').lower() in (
        'xgs12', 'xgs2', 'xm', 'xg', 'ixvm', 'aps_m1010', 'aps_m8400', 'aps_standalone',
    )


def chassis_fabric_port_groups(
    node_id: str,
    *,
    slots: List[Dict[str, Any]],
    ocs_ports: List[Dict[str, Any]],
    dac_ports: List[Dict[str, Any]],
    chassis_type: str = '',
    health_by_name: Optional[Dict[str, Dict]] = None,
    health_by_index: Optional[Dict[int, Dict]] = None,
) -> List[Dict[str, Any]]:
    """
    Chassis Port Fabric: AresONE gets site-mapped OCS ports + trimmed DAC RGs;
    XGS12/IxOS gets per-slot · RG groups from slot_layout (no KCOS).
    """
    groups: List[Dict[str, Any]] = []
    is_aresone = 'aresone' in (chassis_type or '').lower()
    is_ixos = _is_ixos_chassis_type(chassis_type)
    has_ocs_mapping = any(
        p.get('ocs_triplet') or p.get('health') not in ('unused', None, '')
        for p in ocs_ports
    )
    slots_use_fanout = slots_have_ocs_fanout_legs(slots)
    if (
        ocs_ports
        and has_ocs_mapping
        and (is_aresone or not is_ixos)
        and not slots_use_fanout
    ):
        groups.append({
            'label': 'OCS-Facing Ports',
            'role': 'ocs',
            'ports': list(ocs_ports),
        })
    sane = _chassis_slots_sane(slots, chassis_type)
    if slots:
        for g in slot_layout_to_port_groups(
            node_id, slots, health_by_name, health_by_index,
        ):
            if g.get('role') == 'ocs' and groups and groups[0].get('label') == 'OCS-Facing Ports':
                continue
            ports = list(g.get('ports') or [])
            if is_aresone and len(ports) > 2:
                ports = ports[:2]
            if not ports:
                continue
            groups.append({**g, 'ports': ports})
    elif dac_ports:
        groups.append({
            'label': 'DAC / Direct Ports',
            'role': 'dac',
            'ports': list(dac_ports),
        })
    if not groups and slots:
        return slot_layout_to_port_groups(
            node_id, slots, health_by_name, health_by_index,
        )
    return groups


def compact_fabric_port_groups(
    node_type: str,
    port_groups: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Collapse verbose switch groups for Port Fabric rendering.

    OCS uses ocs_shelves on the device object — not compacted here.
    """
    if not port_groups:
        return port_groups

    if node_type == 'ocs':
        return port_groups

    if node_type == 'switch' and len(port_groups) > 6:
        merged: List[Dict[str, Any]] = []
        ocs_ports: List[Dict] = []
        dac_ports: List[Dict] = []
        for pg in port_groups:
            if pg.get('role') == 'ocs':
                ocs_ports.extend(pg.get('ports') or [])
            else:
                dac_ports.extend(pg.get('ports') or [])
        if ocs_ports:
            merged.append({'label': 'OCS Uplink', 'role': 'ocs', 'ports': ocs_ports})
        if dac_ports:
            merged.append({'label': 'DAC / Fabric', 'role': 'dac', 'ports': dac_ports})
        return merged or port_groups

    return port_groups


def persist_layout_on_node(node: LabTopologyNode, layout: Dict[str, Any]) -> None:
    extra = dict(node.extra or {})
    if layout.get('slots'):
        extra['slot_layout'] = layout['slots']
    if layout.get('flat_ports'):
        extra['port_details'] = layout['flat_ports']
        extra['ports'] = [p.get('name') for p in layout['flat_ports'] if p.get('name')]
    node.extra = extra
    node.save(update_fields=['extra'])


def load_site_port_data() -> Dict[str, Dict[str, Dict]]:
    """site_port_data[ip][port_label] = {triplets, role}."""
    import json
    import re
    from collections import defaultdict
    from pathlib import Path

    out: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    site_dir = Path(__file__).resolve().parent.parent / 'resources'
    for sf in sorted(site_dir.glob('ocs_photonic_site*.json')):
        try:
            sd = json.loads(sf.read_text())
        except Exception:
            continue
        for sw in sd.get('arista_switches') or []:
            sip = sw.get('ip', '')
            fm = (sw.get('fixed_mapping') or {}).get('port_to_ocs_triplets') or {}
            for pl, ts in fm.items():
                if not isinstance(ts, list):
                    ts = [ts]
                out[sip][pl] = {'triplets': [str(t) for t in ts], 'role': 'ocs'}
        for ch in sd.get('keysight_chassis') or []:
            chip = ch.get('ip', '')
            fm = (ch.get('fixed_mapping') or {}).get('port_to_ocs_triplets') or {}
            if not fm:
                notes = ch.get('notes', '')
                m = re.search(r'\[ocs_site_mapping\]\s*(\{.+\})', notes, re.DOTALL)
                if m:
                    try:
                        fm = json.loads(m.group(1)).get('port_to_ocs_triplets') or {}
                    except Exception:
                        pass
            for pl, ts in fm.items():
                if not isinstance(ts, list):
                    ts = [ts]
                out[chip][pl] = {'triplets': [str(t) for t in ts], 'role': 'ocs'}
    return dict(out)


def fetch_triplet_to_xcon_for_topo(topo: LabTopology) -> Dict[str, Dict]:
    """Live OCS crossconnect health keyed by normalized triplet."""
    from . import ocs_helpers

    ocs_node = topo.nodes.filter(node_type='ocs').select_related('device').first()
    if not ocs_node or not ocs_node.device:
        return {}
    try:
        from .drivers import get_driver
        drv = get_driver(ocs_node.device)
        rows = drv.fetch_crossconnect_list()
        xcons = drv.get_ocs_crossconnects(raw_rows=rows).data or []
    except Exception:
        return {}

    alarm_codes = {'CR', 'MJ', 'MN'}
    result: Dict[str, Dict] = {}
    for xc in xcons:
        ta = ocs_helpers.norm_ocs_triplet_key(xc.get('port_a', ''))
        tb = ocs_helpers.norm_ocs_triplet_key(xc.get('port_b', ''))
        h2 = xc.get('h2', {})
        alarm = h2.get('alarm', 'CL') in alarm_codes or h2.get('oc', 'OK') not in ('OK', '')
        health = 'alarm' if alarm else 'active'
        loss = h2.get('loss')
        try:
            loss = float(loss) if loss not in (None, '') else None
            if loss is not None and loss < -80:
                loss = None
        except (ValueError, TypeError):
            loss = None
        path_id = xc.get('name', f'{ta}-{tb}')
        for t, peer in [(ta, tb), (tb, ta)]:
            result[t] = {'peer': peer, 'health': health, 'loss_db': loss, 'path_id': path_id}
    return result


def ocs_health_by_triplet(triplet_to_xcon: Dict[str, Dict], triplet_map: Dict) -> Dict[str, Dict]:
    """Fabric health entries keyed by triplet name."""
    health: Dict[str, Dict] = {}
    for t, xc in triplet_to_xcon.items():
        health[t] = {
            'health': 'active',
            'role': 'ocs',
            'link_up': True,
            'ocs_triplet': t,
            'ocs_path_id': xc.get('path_id', ''),
            'loss_db': xc.get('loss_db'),
        }
    return health
