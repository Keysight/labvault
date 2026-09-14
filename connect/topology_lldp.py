"""
SNMP LLDP fetch for Keysight chassis (used in cross-device topology).
Reuses LLDP-MIB (IEEE 802.1AB) logic.
"""
import logging
import re
from connect.snmp_utils import snmp_walk

logger = logging.getLogger(__name__)

OID_LLDP_REM_CHASSIS_ID = '1.0.8802.1.1.2.1.4.1.1.5'
OID_LLDP_REM_PORT_ID = '1.0.8802.1.1.2.1.4.1.1.7'
OID_LLDP_REM_SYS_NAME = '1.0.8802.1.1.2.1.4.1.1.9'
OID_LLDP_LOC_PORT_TABLE = '1.0.8802.1.1.2.1.3.7.1.1.1'


def normalize_chassis_id_for_match(chassis_id):
    """Normalize LLDP chassis-id (often a MAC) for matching. Returns colon-separated lowercase hex."""
    if not chassis_id or not isinstance(chassis_id, str):
        return ''
    s = chassis_id.strip().lower()
    hex_chars = ''.join(c for c in s if c in '0123456789abcdef')
    if len(hex_chars) == 12:  # MAC
        return ':'.join(hex_chars[i:i + 2] for i in range(0, 12, 2))
    return s


def _port_key_variants(local_port_str):
    """Return a set of key forms for a local_port string so that LLDP lookup matches
    regardless of formatting (e.g. 'Port 1.1', '1.1', 'Port1', 'Card 1 Port 1', etc.)."""
    lp = (local_port_str or '').strip()
    if not lp:
        return set()
    keys = {lp, lp.lower(), lp.replace(' ', '')}
    bare = re.sub(r'^[Pp]ort\s*', '', lp).strip()
    if bare:
        keys.update({bare, bare.lower(), f'Port {bare}', f'port {bare}'})
    card_port_m = re.match(r'^card\s+(\d+)\s+port\s+(\d+)$', lp, re.I)
    if card_port_m:
        cn, pn = card_port_m.group(1), card_port_m.group(2)
        keys.update({
            f'{cn}/{pn}', f'{cn}/{pn}'.lower(),
            f'{cn}.{pn}', f'{cn}.{pn}'.lower(),
            f'Card {cn} Port {pn}', f'card {cn} port {pn}',
            f'c{cn}p{pn}', f'C{cn}P{pn}',
        })
    slash_m = re.match(r'^(\d+)/(\d+)$', lp)
    if slash_m:
        cn, pn = slash_m.group(1), slash_m.group(2)
        keys.update({
            f'Card {cn} Port {pn}', f'card {cn} port {pn}',
            f'Port {cn}.{pn}', f'{cn}.{pn}',
            f'c{cn}p{pn}',
        })
    dot_m = re.match(r'^(\d+)\.(\d+)$', lp)
    if dot_m:
        cn, pn = dot_m.group(1), dot_m.group(2)
        keys.update({
            f'{cn}/{pn}', f'Card {cn} Port {pn}',
            f'c{cn}p{pn}',
        })
    node_iface_m = re.match(r'^([^:]+):(.+)$', lp)
    if node_iface_m:
        keys.add(node_iface_m.group(2))
        keys.add(node_iface_m.group(2).lower())
    return keys


def _dedupe_neighbors(rows):
    out = []
    seen = set()
    for nbr in rows or []:
        if not isinstance(nbr, dict):
            continue
        sig = (
            nbr.get('local_port', ''),
            nbr.get('remote_device', ''),
            nbr.get('remote_port', ''),
            nbr.get('mgmt_ip', ''),
        )
        if sig in seen:
            continue
        seen.add(sig)
        out.append(nbr)
    return out


def kcos_connection_local_port(port_row: dict) -> str:
    """Map a KCOS ``/introspection/connections`` port row to a BPS ``slot.lane`` label.

    On M8400 compute nodes, ``eaglefp{N}fo{L}`` maps to lane ``{slot}.{N}`` where
    *slot* is the front-panel physical port id from the connection record.
    """
    if not isinstance(port_row, dict):
        return ''
    try:
        slot_raw = port_row.get('card_number')
        if slot_raw is None:
            slot_raw = port_row.get('slot', 0)
        slot = int(slot_raw or 0)
    except (TypeError, ValueError):
        slot = 0

    from_iface = (
        port_row.get('from_interface')
        or port_row.get('from')
        or port_row.get('type')
        or ''
    ).strip()
    m = re.match(r'^eaglefp(\d+)fo(\d+)$', from_iface, re.I)
    if slot and m:
        return f'{slot}.{int(m.group(1))}'

    to_field = (port_row.get('to_switch_port') or port_row.get('to') or '').strip()
    if slot and to_field:
        if '.' in to_field:
            parts = to_field.split('.')
            try:
                to_slot = int(parts[0])
                lane = int(parts[-1])
                if to_slot == slot:
                    return f'{slot}.{lane}'
            except (ValueError, IndexError):
                pass
            try:
                return f'{slot}.{int(parts[-1])}'
            except (ValueError, IndexError):
                pass
        try:
            return f'{slot}.{int(to_field)}'
        except ValueError:
            pass

    pn = port_row.get('port_number')
    if slot and pn is not None and str(pn).strip() != '':
        try:
            return f'{slot}.{int(pn)}'
        except (TypeError, ValueError):
            pass
    return ''


def synthesize_kcos_b2b_lldp_neighbors(
    port_rows,
    *,
    hostname: str = '',
    mgmt_ip: str = '',
) -> list:
    """Synthetic LLDP rows for same-chassis B2B links (shared transceiver serial).

    When front-panel ``lldpd`` does not report a neighbor on a loopback/DAC B2B
    leg, matching serial numbers on UP connections still let the UI show the peer
    port on the same chassis (same pattern as IxOS same-chassis LLDP B2B).
    """
    from collections import defaultdict

    from .topology_dac_finder import normalize_serial

    host = (hostname or '').strip() or (mgmt_ip or '').strip() or 'localhost'
    entries: list[dict] = []
    for p in port_rows or []:
        if not isinstance(p, dict):
            continue
        link = (p.get('link_state') or p.get('link') or p.get('link_state_raw') or '').upper()
        if link and link not in ('UP', 'ACTIVE', 'LINK_UP'):
            continue
        serial = normalize_serial(p.get('transceiver_serial') or p.get('vendorSerialNumber'))
        if not serial:
            continue
        local = kcos_connection_local_port(p)
        if not local:
            continue
        node_name = (p.get('node_name') or p.get('name') or '').strip()
        from_iface = (
            p.get('from_interface') or p.get('from') or p.get('type') or ''
        ).strip()
        entries.append({
            'serial': serial,
            'local_port': local,
            'node_name': node_name,
            'interface': from_iface,
        })

    by_serial: dict[str, list] = defaultdict(list)
    for entry in entries:
        by_serial[entry['serial']].append(entry)

    neighbors: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for group in by_serial.values():
        if len(group) < 2:
            continue
        # Same serial on one QDD cage → pair legs on that cage only (not a clique
        # across every port that shares a loopback module serial).
        by_card: dict[str, list] = defaultdict(list)
        for entry in group:
            card_key = ''
            for p in port_rows or []:
                if not isinstance(p, dict):
                    continue
                local = kcos_connection_local_port(p)
                if local != entry['local_port']:
                    continue
                try:
                    card_key = str(int(p.get('card_number') or p.get('slot') or 0))
                except (TypeError, ValueError):
                    card_key = str(p.get('card_number') or p.get('slot') or '')
                break
            by_card[card_key or '__unknown__'].append(entry)
        pair_groups = [g for g in by_card.values() if len(g) >= 2]
        if not pair_groups:
            pair_groups = [group]
        for card_group in pair_groups:
            if len(card_group) < 2:
                continue
            for i, src in enumerate(card_group):
                for dst in card_group[i + 1:]:
                    for a, b in ((src, dst), (dst, src)):
                        sig = (a['local_port'], b['local_port'])
                        if sig in seen:
                            continue
                        seen.add(sig)
                        neighbors.append({
                            'local_port': a['local_port'],
                            'remote_device': host,
                            'remote_port': b['local_port'],
                            'chassis_id': '',
                            'mgmt_ip': mgmt_ip or '',
                            'node_name': a['node_name'],
                            'interface': a['interface'],
                            'source': 'b2b_serial',
                            'b2b': True,
                        })
    return neighbors


def _bps_physical_port_in_fanout(pp: dict) -> bool:
    """True when a QDD front-panel cage is broken out (not native 400G-only)."""
    if not isinstance(pp, dict):
        return False
    lanes = pp.get('lanes') or []
    if len(lanes) > 1:
        return True
    mode = (pp.get('currentMode') or '').strip().upper()
    if not mode or mode in ('400G', 'N/A', 'NONE', 'NA'):
        return False
    if re.search(r'1X400', mode):
        return False
    if re.search(r'\d+X100', mode):
        return True
    return False


def merge_lldp_into_cards(cards, lldp_neighbors):
    """Attach LLDP neighbor dicts to the matching port inside each card.

    Sets ``port['lldp'] = [nbr, ...]`` and ``card['has_lldp'] = True`` when
    at least one port in that card has neighbors.

    Matching is done by comparing the LLDP ``local_port`` field against several
    formatting variants of ``card_number/port_number`` and ``port_display``.
    """
    if not lldp_neighbors:
        for card in cards:
            card['has_lldp'] = False
        return

    lldp_by_key = {}
    lldp_by_iface: dict[tuple[str, str], list] = {}
    for nbr in lldp_neighbors:
        for key in _port_key_variants(nbr.get('local_port', '')):
            lldp_by_key.setdefault(key, []).append(nbr)
        node_name = (nbr.get('node_name') or '').strip()
        iface = (nbr.get('interface') or '').strip()
        if node_name and iface:
            lldp_by_iface.setdefault((node_name, iface), []).append(nbr)

    for card in cards:
        card_has = False
        node_name = (card.get('node_name') or '').strip()
        for port in card.get('ports', []):
            cn = port.get('card_number', '')
            pn = port.get('port_number', '')
            pd = port.get('port_display', '')
            candidates = set()
            if cn != '' and pn != '':
                candidates.update(_port_key_variants(f'{cn}/{pn}'))
                candidates.update(_port_key_variants(f'{cn}.{pn}'))
                candidates.update(_port_key_variants(f'N{cn}/P{pn}'))
            candidates.update(_port_key_variants(str(pn)))
            to_field = (port.get('to_switch_port') or '').strip()
            if to_field:
                candidates.update(_port_key_variants(to_field))
            if pd:
                candidates.update(_port_key_variants(pd))
                candidates.update(_port_key_variants(f'Port {pd}'))
            from_iface = (port.get('from_interface') or '').strip()
            matched = []
            seen = set()
            for k in candidates:
                for nbr in lldp_by_key.get(k, []):
                    nid = id(nbr)
                    if nid not in seen:
                        seen.add(nid)
                        matched.append(nbr)
            if from_iface and node_name:
                for nbr in lldp_by_iface.get((node_name, from_iface), []):
                    nid = id(nbr)
                    if nid not in seen:
                        seen.add(nid)
                        matched.append(nbr)
            port['lldp'] = _dedupe_neighbors(matched)
            if port['lldp']:
                card_has = True
        card['has_lldp'] = card_has


def merge_lldp_into_bps_topology(bps_topology, lldp_neighbors, ports=None):
    """Attach LLDP neighbor rows to M8400 BPS physical ports and lanes."""
    if not bps_topology or not lldp_neighbors:
        return

    lldp_by_key = {}
    for nbr in lldp_neighbors:
        lp = (nbr.get('local_port') or '').strip()
        for key in _port_key_variants(lp):
            lldp_by_key.setdefault(key, []).append(nbr)
        # Roll up lane labels (e.g. 1.1) onto physical QDD id (1) for native 400G
        # cages that only expose a single BPS lane (1.0).
        dot_m = re.match(r'^(\d+)\.\d+$', lp)
        if dot_m:
            lldp_by_key.setdefault(dot_m.group(1), []).append(nbr)

    # Map merlin front-panel KCOS ports → physical QDD id (0–7).
    phys_by_merlin_port: dict[int, list[dict]] = {}
    for p in ports or []:
        if (p.get('panel_type') or '') != 'front':
            continue
        try:
            phys_id = int(p.get('port_number', -1))
        except (TypeError, ValueError):
            phys_id = -1
        if phys_id < 0:
            to_field = (p.get('to_switch_port') or '').strip()
            m = re.match(r'^(\d+)\.', to_field)
            if m:
                phys_id = int(m.group(1))
        if phys_id < 0:
            continue
        bucket = phys_by_merlin_port.setdefault(int(phys_id), [])
        for key in _port_key_variants(f'0.{phys_id}'):
            bucket.extend(lldp_by_key.get(key, []))
        for key in _port_key_variants(str(phys_id)):
            bucket.extend(lldp_by_key.get(key, []))

    for slot in bps_topology.get('slots', []):
        for pp in slot.get('physical_ports', []):
            pp_lldp: list[dict] = []
            phys_id = pp.get('id')
            if phys_id is not None:
                for key in _port_key_variants(str(phys_id)):
                    pp_lldp.extend(lldp_by_key.get(key, []))
                for key in _port_key_variants(f'0.{phys_id}'):
                    pp_lldp.extend(lldp_by_key.get(key, []))
                pp_lldp.extend(phys_by_merlin_port.get(int(phys_id), []))
            for lane in pp.get('lanes', []):
                lane_id = (lane.get('id') or '').strip()
                lane_lldp = []
                for key in _port_key_variants(lane_id):
                    lane_lldp.extend(lldp_by_key.get(key, []))
                lane['lldp'] = _dedupe_neighbors(lane_lldp)
                pp_lldp.extend(lane_lldp)
            pp['lldp'] = _dedupe_neighbors(pp_lldp)
            pp['has_lldp'] = bool(pp['lldp'])
            pp['lldp_fanout_only'] = _bps_physical_port_in_fanout(pp)
            # Native 400G: neighbors may use fanout-style labels (1.1) while BPS
            # exposes a single lane (1.0) — show them on that lane chip too.
            if pp['lldp'] and not _bps_physical_port_in_fanout(pp):
                for lane in pp.get('lanes', []):
                    if lane.get('lldp'):
                        continue
                    lane['lldp'] = list(pp['lldp'])


def fetch_chassis_lldp(ip, community='public', port=161, timeout=10):
    """
    Fetch LLDP neighbors from a chassis via SNMP.
    Returns list of dicts: {local_port, remote_device, remote_port, chassis_id, mgmt_ip}
    or empty list on failure.
    """
    try:
        port_names = {}
        for suffix, val in snmp_walk(ip, community, OID_LLDP_LOC_PORT_TABLE, port=port, timeout=timeout) or []:
            if val and suffix:
                parts = suffix.split('.')
                if parts:
                    try:
                        port_num = int(parts[-1])
                        port_names[port_num] = (val or '').strip() or f'Port{port_num}'
                    except (ValueError, IndexError):
                        pass

        chassis_by_suffix = {}
        port_by_suffix = {}
        for s, v in snmp_walk(ip, community, OID_LLDP_REM_CHASSIS_ID, port=port, timeout=timeout) or []:
            if s and v:
                chassis_by_suffix[s] = (v or '').strip()
        for s, v in snmp_walk(ip, community, OID_LLDP_REM_PORT_ID, port=port, timeout=timeout) or []:
            if s and v:
                port_by_suffix[s] = (v or '').strip()

        neighbors = []
        seen = set()
        for suffix, sysname in snmp_walk(ip, community, OID_LLDP_REM_SYS_NAME, port=port, timeout=timeout) or []:
            if not suffix or len(suffix.split('.')) < 2:
                continue
            parts = suffix.split('.')
            try:
                port_num = int(parts[-2])
                rem_idx = int(parts[-1])
                if (port_num, rem_idx) in seen:
                    continue
                seen.add((port_num, rem_idx))
            except (ValueError, IndexError):
                continue

            local_port = port_names.get(port_num, f'Port{port_num}')
            chassis_id = chassis_by_suffix.get(suffix, '')
            port_id = port_by_suffix.get(suffix, '')
            remote_device = (sysname or '').strip() or port_id or chassis_id or 'unknown'
            neighbors.append({
                'local_port': local_port,
                'remote_device': remote_device,
                'remote_port': port_id or '',
                'chassis_id': chassis_id,
                'mgmt_ip': '',
            })
        return neighbors
    except Exception as e:
        logger.warning('Chassis LLDP SNMP failed for %s: %s', ip, e)
        return []
