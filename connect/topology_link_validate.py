"""Validate topology links by flapping one switch port and observing the peer."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .models import Device, KeysightChassis, LabTopologyLink, LabTopologyNode

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.8
POLL_ATTEMPTS = 8
SETTLE_AFTER_FLAP_S = 1.2


def _normalize_port_iface(port: str, vendor: str = 'arista') -> str:
    p = (port or '').strip()
    if not p:
        return ''
    if p.lower().startswith('ethernet'):
        return p
    if p.startswith('port_'):
        num = p.replace('port_', '')
        if vendor in ('arista', 'sonic'):
            return f'Ethernet{num}'
        return p
    if p.startswith('c') and 'p' in p:
        return p
    return p


def _is_link_down(state: str) -> bool:
    s = (state or '').lower()
    return s in ('down', 'disabled', 'notconnect', 'notpresent', 'absent', 'oos', 'admin_down')


def _is_link_up(state: str) -> bool:
    s = (state or '').lower()
    return s in ('up', 'active', 'connected', 'is')


def read_switch_oper_state(device: Device, port: str) -> Tuple[bool, str]:
    """Return (success, oper_state_string)."""
    try:
        from .drivers import get_driver
        drv = get_driver(device)
        if hasattr(drv, 'get_interfaces'):
            result = drv.get_interfaces()
            if result.success and isinstance(result.data, list):
                iface = _normalize_port_iface(port, device.vendor_type)
                for row in result.data:
                    name = row.get('name') or row.get('interface') or ''
                    if name == iface or name == port:
                        return True, (row.get('oper_status') or row.get('link_status') or row.get('state') or '')
        if hasattr(drv, 'get_dom_info'):
            result = drv.get_dom_info()
            if result.success:
                iface = _normalize_port_iface(port, device.vendor_type)
                for row in result.data or []:
                    if (row.get('interface') or '') == iface:
                        return True, 'up' if row.get('rx_power') else 'down'
    except Exception as exc:
        logger.debug('read_switch_oper %s %s: %s', device.ip_address, port, exc)
        return False, str(exc)
    return False, 'unknown'


def flap_switch_port(device: Device, port: str, shutdown: bool) -> Tuple[bool, str]:
    iface = _normalize_port_iface(port, device.vendor_type)
    cmd = f'interface {iface}\n shutdown' if shutdown else f'interface {iface}\n no shutdown'
    try:
        from .drivers import get_driver
        drv = get_driver(device)
        if not hasattr(drv, 'send_config'):
            return False, 'Driver does not support send_config'
        result = drv.send_config([cmd])
        return result.success, result.error or ''
    except Exception as exc:
        return False, str(exc)


def read_chassis_port_link(chassis: KeysightChassis, port_name: str, refresh: bool = False) -> Tuple[bool, str]:
    from .keysight_views import _get_cached, fetch_chassis_data
    from .topology_chassis_ports import build_slot_layout

    cached = None
    if refresh:
        try:
            cached = fetch_chassis_data(chassis)
        except Exception as exc:
            logger.debug('refresh chassis %s: %s', chassis.ip_address, exc)
    if not cached:
        cached = _get_cached(chassis.id) or {}
    layout = build_slot_layout(chassis, cached)
    for p in layout.get('flat_ports') or []:
        if p.get('name') == port_name:
            return True, p.get('link_state') or 'unknown'
    # fallback port_N
    for p in layout.get('flat_ports') or []:
        if str(p.get('port_number')) in port_name:
            return True, p.get('link_state') or 'unknown'
    return False, 'unknown'


def _resolve_endpoints(link: LabTopologyLink) -> Dict[str, Any]:
    na = link.node_a
    nb = link.node_b
    ends = []
    for node, port in ((na, link.port_a), (nb, link.port_b)):
        extra = node.extra or {}
        ends.append({
            'node_id': node.pk,
            'label': node.label,
            'node_type': node.node_type,
            'port': port or '',
            'device': node.device,
            'chassis_id': extra.get('chassis_id'),
            'device_ip': (node.device.ip_address if node.device else '') or extra.get('device_ip', ''),
        })
    return {'a': ends[0], 'b': ends[1], 'link_id': link.pk}


def pick_flap_end(end_a: Dict, end_b: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Prefer switch-bound end as flap side; peer is observed."""
    for flap, peer in ((end_a, end_b), (end_b, end_a)):
        if flap.get('device') and flap.get('node_type') in ('switch', 'firewall', 'ocs'):
            return flap, peer
    return None, None


def validate_link_by_flap(link: LabTopologyLink, *, refresh_peer: bool = True) -> Dict[str, Any]:
    """
    Flap switch (or configurable) port down/up; peer should follow.
    Returns audit dict with validated bool and timeline samples.
    """
    ep = _resolve_endpoints(link)
    flap_end, peer_end = pick_flap_end(ep['a'], ep['b'])
    timeline: List[Dict[str, Any]] = []

    if not flap_end or not flap_end.get('device'):
        return {
            'ok': False,
            'validated': False,
            'error': 'Link validation requires at least one port on a bound switch/firewall device. '
                     'Bind Arista devices or use serial-based discovery for chassis-only links.',
            'timeline': timeline,
        }

    device = flap_end['device']
    flap_port = flap_end['port']
    if not flap_port:
        return {'ok': False, 'validated': False, 'error': 'Flap side has no port name on link', 'timeline': timeline}

    ok0, state0 = read_switch_oper_state(device, flap_port)
    timeline.append({'step': 'baseline_flap', 'state': state0, 'ok': ok0})

    peer_state_before = ''
    peer_ch = None
    if peer_end.get('chassis_id'):
        peer_ch = KeysightChassis.objects.filter(pk=peer_end['chassis_id']).first()
        if peer_ch and peer_end.get('port'):
            _, peer_state_before = read_chassis_port_link(peer_ch, peer_end['port'], refresh=refresh_peer)
            timeline.append({'step': 'baseline_peer_chassis', 'state': peer_state_before})
    elif peer_end.get('device') and peer_end.get('port'):
        _, peer_state_before = read_switch_oper_state(peer_end['device'], peer_end['port'])
        timeline.append({'step': 'baseline_peer_switch', 'state': peer_state_before})

    ok_down, err_down = flap_switch_port(device, flap_port, shutdown=True)
    timeline.append({'step': 'flap_down', 'ok': ok_down, 'error': err_down or ''})
    if not ok_down:
        return {'ok': False, 'validated': False, 'error': f'Failed to shutdown: {err_down}', 'timeline': timeline}

    time.sleep(SETTLE_AFTER_FLAP_S)
    ok1, state_down = read_switch_oper_state(device, flap_port)
    timeline.append({'step': 'after_down_flap', 'state': state_down, 'ok': ok1})

    peer_down = ''
    if peer_ch and peer_end.get('port'):
        _, peer_down = read_chassis_port_link(peer_ch, peer_end['port'], refresh=True)
    elif peer_end.get('device') and peer_end.get('port'):
        _, peer_down = read_switch_oper_state(peer_end['device'], peer_end['port'])
    timeline.append({'step': 'peer_after_down', 'state': peer_down})

    ok_up, err_up = flap_switch_port(device, flap_port, shutdown=False)
    timeline.append({'step': 'flap_up', 'ok': ok_up, 'error': err_up or ''})
    time.sleep(SETTLE_AFTER_FLAP_S)

    ok2, state_up = read_switch_oper_state(device, flap_port)
    timeline.append({'step': 'after_up_flap', 'state': state_up, 'ok': ok2})

    peer_up = ''
    if peer_ch and peer_end.get('port'):
        _, peer_up = read_chassis_port_link(peer_ch, peer_end['port'], refresh=True)
    elif peer_end.get('device') and peer_end.get('port'):
        _, peer_up = read_switch_oper_state(peer_end['device'], peer_end['port'])
    timeline.append({'step': 'peer_after_up', 'state': peer_up})

    flap_went_down = _is_link_down(state_down) or (ok0 and _is_link_up(state0) and not _is_link_up(state_down))
    peer_followed = False
    if peer_state_before or peer_down:
        if _is_link_up(peer_state_before) and _is_link_down(peer_down):
            peer_followed = True
        elif peer_state_before and peer_down and peer_state_before != peer_down:
            peer_followed = True
    flap_restored = _is_link_up(state_up)

    validated = flap_went_down and peer_followed and flap_restored

    extra = dict(link.extra or {})
    extra['last_validation'] = {
        'validated': validated,
        'flap_went_down': flap_went_down,
        'peer_followed': peer_followed,
        'flap_restored': flap_restored,
    }
    link.extra = extra
    link.save(update_fields=['extra'])

    return {
        'ok': True,
        'validated': validated,
        'flap_went_down': flap_went_down,
        'peer_followed': peer_followed,
        'flap_restored': flap_restored,
        'flap_end': flap_end['label'],
        'flap_port': flap_port,
        'peer_end': peer_end['label'],
        'peer_port': peer_end.get('port') or '',
        'timeline': timeline,
        'message': (
            'Link validated — peer followed flap and ports restored.'
            if validated else
            'Validation inconclusive — check timeline (peer may not have followed flap, or cache stale).'
        ),
    }
