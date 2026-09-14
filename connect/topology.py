"""
LLDP Topology Engine
====================
Builds a proper topology graph by correlating LLDP neighbor data across all devices
and Keysight chassis. Uses multi-field matching (hostname, chassis-id, management-IP).
Resolves port-channel memberships to aggregate links.
Stores discovered links in TopologyLink (device<->device) and ChassisDeviceLink
(chassis<->device) for fast rendering.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from django.utils import timezone

from .models import Device, KeysightChassis, TopologyLink, ChassisDeviceLink
from .lldp_persistence import LLDP_RETENTION_SECONDS, get_entity_neighbors, persist_entity_neighbors
from .drivers import get_driver
from .ip_addressing import identity_address_keys
from .topology_lldp import fetch_chassis_lldp, normalize_chassis_id_for_match

logger = logging.getLogger(__name__)

# Node ID prefix for chassis (to distinguish from device IDs)
CHASSIS_NODE_PREFIX = 'chassis-'


def discover_topology():
    """
    Full topology discovery:
    1. Enumerate ALL devices + Keysight chassis as nodes
    2. Fetch LLDP from online devices (REST/SNMP) and online chassis (SNMP)
    3. Build identity index (hostname, IP, chassis-id -> Device or KeysightChassis)
    4. Match neighbors to Device or KeysightChassis
    5. Store TopologyLink (device<->device) and ChassisDeviceLink (chassis<->device)
    """
    all_devices = list(Device.objects.exclude(status='maintenance'))
    all_chassis = list(KeysightChassis.objects.all())
    if not all_devices and not all_chassis:
        return {'nodes': [], 'links': [], 'errors': []}

    online_devices = [d for d in all_devices if d.status == 'online']
    online_chassis = [c for c in all_chassis if c.status == 'online']

    # Identity index: key -> (Device | KeysightChassis)
    identity_index = {}
    for d in all_devices:
        if d.hostname:
            identity_index[d.hostname.lower()] = d
            identity_index[d.hostname.split('.')[0].lower()] = d
        for key in identity_address_keys(
            ipv4=d.ip_address,
            ipv6=getattr(d, 'mgmt_ipv6', '') or '',
            hostname=d.hostname or '',
        ):
            identity_index[key] = d
        if d.mac_address:
            identity_index[d.mac_address.lower()] = d
    for c in all_chassis:
        if c.hostname:
            identity_index[c.hostname.lower()] = c
            identity_index[c.hostname.split('.')[0].lower()] = c
        for key in identity_address_keys(
            ipv4=c.ip_address,
            ipv6=getattr(c, 'mgmt_ipv6', '') or '',
            hostname=c.hostname or '',
        ):
            identity_index[key] = c
        # AresONE / HTREX / T-Rex: LLDP often reports "ares1-{serial}" (index when serial present)
        ct = (getattr(c, 'chassis_type', '') or '')
        if c.serial_number and ct in ('aresone', 'aresone_htrex', 'trex'):
            for fmt in (f'ares1-{c.serial_number.lower()}', f'ares1-{c.serial_number}'):
                if fmt:
                    identity_index[fmt.lower()] = c

    # Fetch LLDP from devices
    lldp_data = {}
    pc_data = {}
    errors = []
    scanned_device_ids = set()

    def _fetch_device_lldp(device):
        try:
            driver = get_driver(device)
            lldp_result = driver.get_lldp_neighbors_detail()
            pc_result = driver.get_port_channel_members()
            return 'device', device.id, (
                lldp_result.data if lldp_result.success else [],
                pc_result.data if pc_result.success else {},
            ), None
        except Exception as e:
            logger.warning('Topology: LLDP fetch failed for %s (%s): %s',
                           device.hostname or device.ip_address, device.ip_address, e)
            return 'device', device.id, ([], {}), str(e)

    def _fetch_chassis_lldp_task(chassis):
        """Prefer SSH ``show lldp-peer-info`` (same as chassis detail); SNMP is fallback only.

        Keysight AresONE/XGS management often has SSH open while SNMP LLDP-MIB is
        filtered — SNMP-only scans looked like "LLDP is broken" on customer VMs.
        """
        err = None
        neighbors: list = []
        try:
            from .keysight_drivers import get_driver as get_chassis_driver
            drv = get_chassis_driver(chassis)
            if hasattr(drv, 'get_lldp_ssh'):
                bps_topology = None
                if (getattr(chassis, 'chassis_type', '') or '') == 'aps_m8400':
                    try:
                        from .keysight_drivers.bps import BPSDriver
                        bps_res = BPSDriver(
                            chassis.ip_address, chassis.username, chassis.password,
                        ).get_topology()
                        if getattr(bps_res, 'success', False) and isinstance(bps_res.data, dict):
                            bps_topology = bps_res.data
                    except Exception:
                        pass
                res = drv.get_lldp_ssh(
                    bps_topology=bps_topology,
                    chassis_type=getattr(chassis, 'chassis_type', '') or '',
                )
                if getattr(res, 'success', False) and res.data:
                    return 'chassis', chassis.id, list(res.data), None
                err = (
                    getattr(res, 'error', None)
                    or getattr(drv, '_last_ssh_error', None)
                    or 'SSH LLDP returned no neighbors'
                )
        except Exception as e:
            err = str(e)
            logger.debug('Topology: chassis SSH LLDP %s: %s', chassis.ip_address, e)

        try:
            neighbors = fetch_chassis_lldp(
                chassis.ip_address,
                community=getattr(chassis, 'snmp_community', '') or 'public',
                timeout=3,
            ) or []
            if neighbors:
                return 'chassis', chassis.id, neighbors, None
        except Exception as e:
            err = f'{err+"; " if err else ""}SNMP: {e}'
            logger.warning('Topology: Chassis LLDP failed for %s: %s', chassis.ip_address, e)
        return 'chassis', chassis.id, neighbors or [], err

    chassis_lldp = {}  # chassis_id -> [neighbors]
    scanned_chassis_ids = set()

    total_tasks = len(online_devices) + len(online_chassis)
    if total_tasks > 0:
        # SSH chassis LLDP is slower than SNMP; scale timeout with inventory size.
        max_workers = min(12, max(1, total_tasks))
        batch_timeout = max(120, 15 * ((total_tasks + max_workers - 1) // max_workers))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for d in online_devices:
                futures[pool.submit(_fetch_device_lldp, d)] = d
            for c in online_chassis:
                futures[pool.submit(_fetch_chassis_lldp_task, c)] = c
            try:
                for future in as_completed(futures, timeout=batch_timeout):
                    try:
                        kind, oid, payload, err = future.result()
                        if kind == 'device':
                            lldp, pc = payload
                            dev = futures[future]
                            # Scan completed successfully even when the neighbor list is empty.
                            lldp_data[oid] = persist_entity_neighbors(
                                'device',
                                oid,
                                lldp or [],
                                fresh_scan_ok=err is None,
                            )
                            pc_data[oid] = pc
                            scanned_device_ids.add(oid)
                            if err:
                                errors.append(f"{dev.hostname or dev.ip_address}: {err}")
                        else:
                            ch = futures[future]
                            merged = persist_entity_neighbors(
                                'chassis',
                                oid,
                                payload or [],
                                fresh_scan_ok=err is None and bool(payload),
                            )
                            chassis_lldp[oid] = merged
                            scanned_chassis_ids.add(oid)
                            if err and not payload:
                                errors.append(f"{ch.hostname or ch.ip_address} (chassis): {err}")
                    except Exception as e:
                        obj = futures[future]
                        label = getattr(obj, 'ip_address', str(obj))
                        errors.append(f"{label}: {e}")
            except TimeoutError:
                logger.warning('Topology: parallel LLDP batch timed out; partial results only')
                errors.append('Topology scan timed out (partial results)')

    # Build port-channel map for devices
    pc_member_map = {}
    for dev_id, pcs in pc_data.items():
        pc_member_map[dev_id] = {}
        for pc_name, members in pcs.items():
            for member in members:
                pc_member_map[dev_id][member] = pc_name

    device_by_id = {d.id: d for d in all_devices}
    chassis_by_id = {c.id: c for c in all_chassis}

    # Use 24h persisted LLDP when live fetch missed an endpoint
    for d in all_devices:
        if d.id not in lldp_data:
            persisted = get_entity_neighbors('device', d.id)
            if persisted:
                lldp_data[d.id] = persisted
    for c in all_chassis:
        if c.id not in chassis_lldp:
            persisted = get_entity_neighbors('chassis', c.id)
            if persisted:
                chassis_lldp[c.id] = persisted

    # Device<->Device links (TopologyLink)
    links = []
    seen_pairs = set()

    for dev_id, neighbors in lldp_data.items():
        device_a = device_by_id.get(dev_id)
        if not device_a:
            continue
        for nbr in neighbors:
            remote = _resolve_neighbor(nbr, identity_index)
            if not remote or remote is device_a:
                continue
            local_port = nbr.get('local_port', '')
            remote_port = nbr.get('remote_port', '')
            if isinstance(remote, KeysightChassis):
                continue  # chassis link handled below
            lag_a = pc_member_map.get(device_a.id, {}).get(local_port, '')
            lag_b = pc_member_map.get(remote.id, {}).get(remote_port, '')
            lag = lag_a or lag_b
            pair_key = _dedup_key(device_a.id, local_port, remote.id, remote_port)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            links.append({
                'device_a': device_a,
                'port_a': local_port,
                'device_b': remote,
                'port_b': remote_port,
                'lag': lag,
            })

    # Chassis<->Device links (ChassisDeviceLink)
    chassis_links = []
    chassis_seen = set()

    for ch_id, neighbors in chassis_lldp.items():
        chassis = chassis_by_id.get(ch_id)
        if not chassis:
            continue
        for nbr in neighbors:
            remote = _resolve_neighbor(nbr, identity_index)
            if not remote or remote is chassis:
                continue
            port_chassis = nbr.get('local_port', '')
            port_device = nbr.get('remote_port', '')
            if isinstance(remote, KeysightChassis):
                continue  # chassis<->chassis not stored for now
            key = (chassis.id, port_chassis, remote.id, port_device)
            if key in chassis_seen:
                continue
            chassis_seen.add(key)
            chassis_links.append({
                'chassis': chassis,
                'device': remote,
                'port_chassis': port_chassis,
                'port_device': port_device,
            })

    # Also: Device LLDP neighbor that resolved to chassis
    for dev_id, neighbors in lldp_data.items():
        device_a = device_by_id.get(dev_id)
        if not device_a:
            continue
        for nbr in neighbors:
            remote = _resolve_neighbor(nbr, identity_index)
            if not remote or not isinstance(remote, KeysightChassis):
                continue
            local_port = nbr.get('local_port', '')
            remote_port = nbr.get('remote_port', '')
            key = (remote.id, remote_port, device_a.id, local_port)
            if key in chassis_seen:
                continue
            chassis_seen.add(key)
            chassis_links.append({
                'chassis': remote,
                'device': device_a,
                'port_chassis': remote_port,
                'port_device': local_port,
            })

    _store_topology_links(links, scanned_device_ids)
    _store_chassis_links(chassis_links, scanned_chassis_ids, scanned_device_ids)
    supplement_chassis_links_from_switch_lldp()

    # Build nodes: devices + chassis
    nodes = []
    for d in all_devices:
        nodes.append({
            'id': str(d.id),
            'ip': d.ip_address,
            'name': d.hostname or d.ip_address,
            'vendor': d.vendor_type,
            'status': d.status,
            'model': d.model_name or '',
            'color': getattr(d, 'vendor_color', '#607D8B'),
            'icon': getattr(d, 'vendor_icon', 'fa-server'),
            # Device tags (comma field); Keysight chassis use team_tags — same JSON key for filtering UI
            'tags': list(d.tag_list),
        })
    for c in all_chassis:
        nodes.append({
            'id': f'{CHASSIS_NODE_PREFIX}{c.id}',
            'ip': c.ip_address,
            'name': c.hostname or c.ip_address,
            'vendor': 'keysight',
            'status': c.status,
            'model': c.get_chassis_type_display() if hasattr(c, 'get_chassis_type_display') else c.chassis_type,
            'color': '#9C27B0',
            'icon': 'fa-microchip',
            'tags': list(c.team_tags_list),
        })

    link_list = []
    for l in links:
        link_list.append({
            'source': str(l['device_a'].id),
            'target': str(l['device_b'].id),
            'local_port': l['port_a'],
            'remote_port': l['port_b'],
            'lag': l['lag'],
            'status': 'up',
        })
    for cl in chassis_links:
        link_list.append({
            'source': f'{CHASSIS_NODE_PREFIX}{cl["chassis"].id}',
            'target': str(cl['device'].id),
            'local_port': cl['port_chassis'],
            'remote_port': cl['port_device'],
            'lag': '',
            'status': 'up',
        })

    aggregated = _aggregate_parallel_links(link_list)
    return {'nodes': nodes, 'links': aggregated, 'errors': errors}


def supplement_chassis_links_from_switch_lldp():
    """
    Build ChassisDeviceLink rows from Arista/SONiC switch LLDP tables.

    Switch LLDP lists chassis hostnames as neighbors; this supplements chassis-side
    SNMP/SSH discovery when chassis polls fail or time out.
    """
    import os
    import re

    from .lldp_persistence import load_switch_lldp_by_ip

    _SWITCH_LLDP_RAW = {}
    _RAW_DIR = ''

    def _parse_txt(fpath):
        out = []
        try:
            with open(fpath, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    line = line.rstrip()
                    if not line or not line[0].isalpha():
                        continue
                    parts = re.split(r'\s{2,}', line.strip())
                    if len(parts) >= 3 and parts[0].startswith('Ethernet'):
                        out.append({
                            'local_port': parts[0],
                            'remote_device': parts[1].split('.')[0],
                            'remote_port': parts[2],
                        })
        except OSError:
            pass
        return out

    identity_index = {}
    for c in KeysightChassis.objects.all():
        for key in (c.hostname, c.ip_address, f'ares1-{c.serial_number}'.lower() if c.serial_number else ''):
            if key:
                identity_index[key.lower().split('.')[0]] = c
                identity_index[key.lower().replace('_', '').replace('-', '').replace(' ', '')] = c
        if c.serial_number and (c.chassis_type or '') in ('aresone', 'aresone_htrex', 'trex'):
            identity_index[f'ares1-{c.serial_number.lower()}'] = c

    now = timezone.now()
    persisted = load_switch_lldp_by_ip()
    switches = {
        d.ip_address: d
        for d in Device.objects.filter(vendor_type__in=('arista', 'sonic'))
        if d.ip_address
    }
    for ip in persisted:
        if ip not in switches:
            extra = Device.objects.filter(ip_address=ip).first()
            if extra:
                switches[ip] = extra

    for sw_ip, sw_dev in switches.items():
        neighbors = list(persisted.get(sw_ip) or [])
        if not neighbors:
            fname = _SWITCH_LLDP_RAW.get(sw_ip, '')
            if fname:
                fpath = os.path.join(_RAW_DIR, fname)
                if os.path.isfile(fpath):
                    neighbors = _parse_txt(fpath)
        for nbr in neighbors:
            remote_name = (nbr.get('remote_device') or '').strip()
            if not remote_name:
                continue
            keys = [
                remote_name.lower(),
                remote_name.lower().split('.')[0],
                remote_name.lower().replace('_', '').replace('-', '').replace(' ', ''),
            ]
            chassis = None
            for k in keys:
                obj = identity_index.get(k)
                if isinstance(obj, KeysightChassis):
                    chassis = obj
                    break
            if not chassis:
                continue
            port_sw = nbr.get('local_port') or ''
            port_ch = nbr.get('remote_port') or ''
            if not port_sw:
                continue
            ChassisDeviceLink.objects.update_or_create(
                chassis=chassis,
                port_chassis=port_ch,
                device=sw_dev,
                port_device=port_sw,
                defaults={'link_status': 'up', 'last_seen': now, 'discovered_via': 'lldp'},
            )


def get_cached_topology():
    """Return topology from database (fast, no live queries).

    Includes devices and Keysight chassis as nodes, with device<->device
    and chassis<->device links. Links stay visible for 24h after last_seen
    even if a scan temporarily marks them down.
    """
    # Seed switch-derived chassis links when DB is sparse (e.g. after timeout scans)
    if ChassisDeviceLink.objects.filter(link_status='up').count() < 8:
        try:
            supplement_chassis_links_from_switch_lldp()
        except Exception as exc:
            logger.warning('supplement_chassis_links_from_switch_lldp: %s', exc)

    cutoff = timezone.now() - timedelta(seconds=LLDP_RETENTION_SECONDS)
    devices = Device.objects.exclude(status='maintenance')
    chassis_list = list(KeysightChassis.objects.all())
    node_ids = set()
    nodes = []
    for d in devices:
        node_ids.add(str(d.id))
        nodes.append({
            'id': str(d.id),
            'ip': d.ip_address,
            'name': d.hostname or d.ip_address,
            'vendor': d.vendor_type,
            'status': d.status,
            'model': d.model_name or '',
            'color': getattr(d, 'vendor_color', '#607D8B'),
            'icon': getattr(d, 'vendor_icon', 'fa-server'),
            'tags': list(d.tag_list),
        })
    for c in chassis_list:
        cid = f'{CHASSIS_NODE_PREFIX}{c.id}'
        node_ids.add(cid)
        nodes.append({
            'id': cid,
            'ip': c.ip_address,
            'name': c.hostname or c.ip_address,
            'vendor': 'keysight',
            'status': c.status,
            'model': c.get_chassis_type_display() if hasattr(c, 'get_chassis_type_display') else c.chassis_type,
            'color': '#9C27B0',
            'icon': 'fa-microchip',
            'tags': list(c.team_tags_list),
        })

    links = []
    for tl in TopologyLink.objects.select_related('device_a', 'device_b').all():
        src, dst = str(tl.device_a.id), str(tl.device_b.id)
        if src not in node_ids or dst not in node_ids:
            continue
        if tl.link_status != 'up' and tl.last_seen < cutoff:
            continue
        links.append({
            'source': src, 'target': dst,
            'local_port': tl.port_a, 'remote_port': tl.port_b,
            'lag': tl.lag, 'speed': tl.speed, 'status': tl.link_status,
        })
    for cl in ChassisDeviceLink.objects.select_related('chassis', 'device').all():
        src = f'{CHASSIS_NODE_PREFIX}{cl.chassis_id}'
        dst = str(cl.device_id)
        if src not in node_ids or dst not in node_ids:
            continue
        if cl.link_status != 'up' and cl.last_seen < cutoff:
            continue
        links.append({
            'source': src, 'target': dst,
            'local_port': cl.port_chassis, 'remote_port': cl.port_device,
            'lag': '', 'speed': '', 'status': cl.link_status,
        })

    aggregated = _aggregate_parallel_links(links)
    return {'nodes': nodes, 'links': aggregated}


def _resolve_neighbor(nbr, identity_index):
    """Try to match an LLDP neighbor to a known device using multiple fields."""
    # Try system-name / remote_device (hostname match)
    for field in ('remote_device', 'system_name'):
        val = nbr.get(field, '').strip()
        if val:
            # Try exact match
            device = identity_index.get(val.lower())
            if device:
                return device
            # Try short hostname
            short = val.split('.')[0].lower()
            device = identity_index.get(short)
            if device:
                return device

    # Try management-address (IPv4 or IPv6 from LLDP)
    mgmt_ip = (nbr.get('mgmt_ip') or nbr.get('mgmt_ip_raw') or '').strip()
    if mgmt_ip:
        device = identity_index.get(mgmt_ip) or identity_index.get(mgmt_ip.lower())
        if device:
            return device

    # Try chassis-id (MAC match) — raw and normalized colon form
    raw_cid = (nbr.get('chassis_id') or '').strip()
    norm_cid = normalize_chassis_id_for_match(raw_cid).lower() if raw_cid else ''
    for cid in (raw_cid.lower(), norm_cid):
        if cid:
            device = identity_index.get(cid)
            if device:
                return device

    return None


def _dedup_key(id_a, port_a, id_b, port_b):
    """Create canonical key for bidirectional link dedup."""
    if id_a < id_b:
        return (id_a, port_a, id_b, port_b)
    elif id_a > id_b:
        return (id_b, port_b, id_a, port_a)
    else:
        return (id_a, min(port_a, port_b), id_b, max(port_a, port_b))


def _store_chassis_links(links, scanned_chassis_ids, scanned_device_ids):
    """Store ChassisDeviceLink entries. Mark stale as down only after 24h without refresh."""
    now = timezone.now()
    cutoff = now - timedelta(seconds=LLDP_RETENTION_SECONDS)
    existing_ids = set()
    for cl in links:
        obj, _ = ChassisDeviceLink.objects.update_or_create(
            chassis=cl['chassis'],
            port_chassis=cl['port_chassis'],
            device=cl['device'],
            port_device=cl['port_device'],
            defaults={'link_status': 'up', 'last_seen': now},
        )
        existing_ids.add(obj.id)
    stale = ChassisDeviceLink.objects.exclude(id__in=existing_ids).filter(link_status='up')
    for tl in stale:
        if tl.chassis_id in scanned_chassis_ids and tl.device_id in scanned_device_ids:
            if tl.last_seen and tl.last_seen >= cutoff:
                continue
            tl.link_status = 'down'
            tl.save(update_fields=['link_status'])


def _store_topology_links(links, scanned_device_ids=None):
    """Store discovered links in TopologyLink model, updating existing entries.

    Only marks links as 'down' if BOTH endpoint devices were in the scanned set.
    This prevents marking links as down just because one endpoint was offline
    or unreachable during this scan.
    """
    now = timezone.now()
    existing_ids = set()

    for l in links:
        obj, created = TopologyLink.objects.update_or_create(
            device_a=l['device_a'],
            port_a=l['port_a'],
            device_b=l['device_b'],
            port_b=l['port_b'],
            defaults={
                'lag': l.get('lag', ''),
                'discovered_via': 'lldp',
                'link_status': 'up',
                'last_seen': now,
            }
        )
        existing_ids.add(obj.id)

    # Mark old links as down — but only if BOTH devices were scanned
    if scanned_device_ids is None:
        # Legacy behavior: mark all unseen links as down
        stale = TopologyLink.objects.exclude(id__in=existing_ids)
        stale.filter(link_status='up').update(link_status='down')
    else:
        # Only mark links as down when both endpoints were successfully scanned
        # but the link was not found. This prevents false "down" marking when
        # one endpoint is simply unreachable.
        stale = TopologyLink.objects.exclude(id__in=existing_ids).filter(link_status='up')
        cutoff = now - timedelta(seconds=LLDP_RETENTION_SECONDS)
        for tl in stale:
            if tl.device_a_id in scanned_device_ids and tl.device_b_id in scanned_device_ids:
                if tl.last_seen and tl.last_seen >= cutoff:
                    continue
                tl.link_status = 'down'
                tl.save(update_fields=['link_status'])


def _aggregate_parallel_links(links, max_per_pair=12):
    """Aggregate parallel links between the same node pair (cap list length for UI)."""
    pair_map = {}
    for link in links:
        src, dst = link['source'], link['target']
        key = (min(src, dst), max(src, dst))
        pair_map.setdefault(key, []).append(link)

    result = []
    for (src, dst), group in pair_map.items():
        if len(group) == 1:
            group[0]['link_count'] = 1
            result.append(group[0])
            continue
        ports_a = [l['local_port'] for l in group if l.get('local_port')]
        ports_b = [l['remote_port'] for l in group if l.get('remote_port')]
        if len(ports_a) > max_per_pair:
            ports_a = ports_a[:max_per_pair] + [f'+{len(ports_a) - max_per_pair} more']
        if len(ports_b) > max_per_pair:
            ports_b = ports_b[:max_per_pair] + [f'+{len(ports_b) - max_per_pair} more']
        lags = set(l.get('lag', '') for l in group if l.get('lag'))
        result.append({
            'source': group[0]['source'],
            'target': group[0]['target'],
            'local_port': ', '.join(ports_a),
            'remote_port': ', '.join(ports_b),
            'lag': ', '.join(lags) if lags else '',
            'status': 'up' if any(l.get('status') == 'up' for l in group) else 'down',
            'link_count': len(group),
        })

    return result
