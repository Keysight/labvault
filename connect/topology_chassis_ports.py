"""Build slot/port layout for topology designer (matches Keysight chassis detail view)."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List

from .models import KeysightChassis


def _port_name(port: Dict[str, Any]) -> str:
    if port.get('name'):
        name = str(port['name'])
        # Bare digits collide across slots (xgs12 Slot 1 vs Slot 6 both have port "1").
        if name.isdigit() or (port.get('card_number') is not None and re.match(r'^c\d+p\d+$', name, re.I) is None):
            slot = port.get('card_number') or port.get('slot')
            pn = port.get('port_number')
            if slot is not None and pn is not None:
                return f'c{slot}p{pn}'
        return name
    pn = port.get('port_number')
    if port.get('port_display'):
        return str(port['port_display'])
    slot = port.get('card_number') or port.get('slot')
    if pn is not None and slot is not None:
        return f'c{slot}p{pn}'
    if pn is not None:
        return f'port_{pn}'
    return str(port.get('id') or 'port')


def _port_dict(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'name': _port_name(p),
        'port_number': p.get('port_number'),
        'card_number': p.get('card_number'),
        'port_display': p.get('port_display') or '',
        'link_state': p.get('link_state') or 'unknown',
        'led_color': p.get('led_color') or 'off',
        'speed': p.get('speed') or '',
        'serial': (p.get('transceiver_serial') or '').strip(),
        'owner': p.get('owner') or '',
        'panel_type': p.get('panel_type') or '',
    }


def _aresone_resource_groups(ports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """AresONE 9–24 → RG01–RG08 (2 ports per RG), same as Keysight CMC."""
    rg_map: Dict[int, List[Dict]] = defaultdict(list)
    for p in ports:
        pn = p.get('port_number')
        try:
            pn = int(pn)
        except (TypeError, ValueError):
            continue
        if 9 <= pn <= 24:
            rg = (pn - 9) // 2 + 1
            rg_map[rg].append(p)
    out = []
    for rgn in sorted(rg_map.keys()):
        plist = sorted(rg_map[rgn], key=lambda x: x.get('port_number', 0))
        out.append({
            'number': rgn,
            'label': f'RG{rgn:02d}',
            'title': f'Resource Group {rgn:02d} (RG{rgn:02d})',
            'ports': [_port_dict(p) for p in plist],
        })
    return out


def _slot_from_card(card: Dict[str, Any], is_aresone: bool) -> Dict[str, Any]:
    card_num = card.get('card_number', 0)
    try:
        card_num = int(card_num)
    except (TypeError, ValueError):
        card_num = 0
    ctype = (card.get('type') or '').lower()
    slot_entry: Dict[str, Any] = {
        'slot': card_num,
        'label': f'Slot {card_num}' if card_num else 'Chassis',
        'card_type': card.get('type') or '',
        'state': card.get('state') or '',
        'is_mgmt': 'mgmt' in ctype or bool(card.get('node_name')),
        'resource_groups': [],
        'ports': [],
    }
    raw_ports = list(card.get('ports') or [])
    if card.get('resource_groups'):
        for rg in card['resource_groups']:
            rg_ports = [_port_dict(p) for p in (rg.get('ports') or [])]
            slot_entry['resource_groups'].append({
                'number': rg.get('number'),
                'label': rg.get('label') or '',
                'title': rg.get('title') or rg.get('label') or '',
                'ports': rg_ports,
            })
            slot_entry['ports'].extend(rg_ports)
    elif is_aresone and raw_ports:
        slot_entry['resource_groups'] = _aresone_resource_groups(raw_ports)
        for rg in slot_entry['resource_groups']:
            slot_entry['ports'].extend(rg['ports'])
    else:
        for p in sorted(raw_ports, key=lambda x: x.get('port_number', 0)):
            slot_entry['ports'].append(_port_dict(p))
    return slot_entry


def layout_from_port_details(
    port_details: List[Dict[str, Any]],
    *,
    chassis_type: str = '',
    port_names: List[str] | None = None,
) -> Dict[str, Any]:
    """Rebuild slot view from saved extra.port_details or port name list."""
    if port_details:
        ports_raw = []
        for pd in port_details:
            if isinstance(pd, dict):
                ports_raw.append({
                    'port_number': pd.get('port_number'),
                    'card_number': pd.get('card_number', 1),
                    'name': pd.get('name'),
                    'link_state': pd.get('link_state', 'unknown'),
                    'led_color': pd.get('led_color', 'off'),
                    'transceiver_serial': pd.get('serial', ''),
                    'owner': pd.get('owner', ''),
                })
    elif port_names:
        ports_raw = []
        for pname in port_names:
            pn = None
            if isinstance(pname, str) and pname.startswith('port_'):
                try:
                    pn = int(pname.replace('port_', ''))
                except ValueError:
                    pn = None
            ports_raw.append({
                'name': pname,
                'port_number': pn,
                'card_number': 1,
                'link_state': 'unknown',
                'led_color': 'off',
            })
    else:
        return {'slots': [], 'flat_ports': []}

    is_aresone = 'aresone' in (chassis_type or '').lower()
    card = {'card_number': 1, 'type': 'Line Card', 'ports': ports_raw}
    slot = _slot_from_card(card, is_aresone)
    return {'slots': [slot], 'flat_ports': slot['ports']}


def build_slot_layout(chassis: KeysightChassis, cached: Dict[str, Any]) -> Dict[str, Any]:
    """
    Use cards/resource_groups from Keysight cache (same structure as chassis detail page).
    """
    cards = list(cached.get('cards') or [])
    is_aresone = cached.get('is_aresone') or chassis.chassis_type == 'aresone'
    ports = list(cached.get('ports') or [])

    if not cards and ports:
        ports_by_card: Dict[int, List[Dict]] = defaultdict(list)
        for p in ports:
            cn = p.get('card_number', 1)
            try:
                cn = int(cn)
            except (TypeError, ValueError):
                cn = 1
            ports_by_card[cn].append(p)
        cards = [
            {'card_number': cn, 'type': 'Ports', 'ports': sorted(pl, key=lambda x: x.get('port_number', 0))}
            for cn, pl in sorted(ports_by_card.items())
        ]

    slots_out = [_slot_from_card(card, is_aresone) for card in sorted(cards, key=lambda c: c.get('card_number', 0))]

    flat: List[Dict[str, Any]] = []
    seen: set = set()
    for sl in slots_out:
        for p in sl.get('ports') or []:
            key = p.get('name')
            if key and key not in seen:
                seen.add(key)
                flat.append(p)

    return {
        'chassis_id': chassis.pk,
        'ip': chassis.ip_address,
        'hostname': chassis.hostname or chassis.ip_address,
        'chassis_type': chassis.chassis_type,
        'slots': slots_out,
        'flat_ports': flat,
        'source': 'live_cache',
    }


def merge_layout_with_saved(
    live: Dict[str, Any],
    saved_layout: List[Dict[str, Any]] | None,
) -> Dict[str, Any]:
    """Prefer live cache; fall back to persisted slot_layout."""
    if live.get('slots'):
        return live
    if saved_layout:
        flat = []
        for sl in saved_layout:
            for p in sl.get('ports') or []:
                flat.append(p)
        out = dict(live)
        out['slots'] = saved_layout
        out['flat_ports'] = flat
        out['source'] = 'saved_layout'
        return out
    return live
