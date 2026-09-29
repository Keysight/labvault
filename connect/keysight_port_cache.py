"""Build a chassis card grid from a cached IxOS/fleet port list.

The shared chassis cache (``keysight:chassis_data:<id>``) can be written by two
producers: the full ``keysight_views.fetch_chassis_data`` (cards + ports + SSH
topology) and the lighter fleet heartbeat ``_refresh_ports_cache`` (ports only).
These helpers let the dashboard and chassis-detail pages render a card grid
from ports alone, marking synthesized cards with ``_from_fleet_ports`` and the
payload with ``_cards_from_ports`` so callers know a full fetch is still due.
"""
from __future__ import annotations

from collections import defaultdict


def cards_from_cached_ports(ports: list) -> list:
    """Build a chassis-detail card grid from fleet/IxOS port cache.

    Pickup import and live heartbeat write ``ports`` long before ``get_cards`` +
    SSH topology finish. The detail page used to show “No cards detected” even
    when fleet already had the port list.
    """
    by_card: dict = defaultdict(list)
    for p in ports or []:
        if not isinstance(p, dict):
            continue
        cn = p.get('card_number')
        if cn is None:
            cn = 1
        by_card[cn].append(p)

    def _cn_key(cn):
        try:
            return (0, int(cn))
        except (TypeError, ValueError):
            return (1, str(cn))

    cards = []
    for cn in sorted(by_card, key=_cn_key):
        plist = by_card[cn]
        card = {
            'card_number': cn,
            'type': 'IxOS ports (fleet cache)',
            'state': 'ready',
            'serial_number': '',
            'num_ports': len(plist),
            'ports': plist,
            'ports_up': sum(1 for p in plist if p.get('link_state') == 'up'),
            'ports_total': len(plist),
            'ports_owned': sum(1 for p in plist if (p.get('owner') or 'Free') != 'Free'),
            'resource_groups': [],
            '_from_fleet_ports': True,
        }
        rg_map: dict = defaultdict(list)
        for p in plist:
            pn = p.get('port_number')
            try:
                pn_i = int(pn)
            except (TypeError, ValueError):
                continue
            # AresONE numbering: ports 9-24 pair into RG01..RG08.
            if 9 <= pn_i <= 24:
                rg_map[(pn_i - 9) // 2 + 1].append(p)
        for rg_num in sorted(rg_map):
            card['resource_groups'].append({
                'number': rg_num,
                'label': f'RG{rg_num:02d}',
                'title': f'Resource Group {rg_num:02d} (RG{rg_num:02d})',
                'mode_label': '',
                'ports': sorted(rg_map[rg_num], key=lambda x: x.get('port_number', 0)),
            })
        cards.append(card)
    return cards


def cache_has_live_ixos_cards(cached: dict | None) -> bool:
    """True when cards came from get_cards / SSH, not the fleet port fallback."""
    data = cached or {}
    cards = data.get('cards') or []
    if not cards or data.get('_cards_from_ports'):
        return False
    return not all(isinstance(c, dict) and c.get('_from_fleet_ports') for c in cards)


def hydrate_cards_from_port_cache(cached: dict | None) -> dict:
    """If IxOS cards are missing, render the fleet port cache as cards."""
    data = dict(cached or {})
    cards = data.get('cards') or []
    ports = data.get('ports') or []
    if cards or not ports:
        return data
    data['cards'] = cards_from_cached_ports(ports)
    data['_cards_from_ports'] = True
    if not data.get('total_ports'):
        data['total_ports'] = len(ports)
    return data
