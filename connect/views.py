"""
LabVault Multi-Vendor Views
Supports: Arista EOS, SONiC, FortiGate, Palo Alto

Performance: Background thread caches device data every 20s.
Page loads serve from cache (instant). No blocking API calls on page load.

Enhanced features: BGP/OSPF/Environment tabs, topology engine, fleet reports,
config management, webhooks, API tokens, maintenance windows, scheduled jobs.
"""
import csv
import io
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import difflib
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from django.db import close_old_connections
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET, require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.contrib import messages
from django.db.models import Count, Q, Avg, Sum, Max, Min, F

from .models import (Device, AuditLog, DeviceSnapshot, Alert,
                     ConfigBackup, ComplianceRule, ComplianceResult, SavedCommand,
                     TopologyLink, InterfaceSnapshot, ScheduledJob,
                     MaintenanceWindow, MaintenanceWindowDevice,
                     WebhookEndpoint, APIToken, DeviceGroup, RequestLog,
                     OcsPatchSnapshot)
from .forms import ConnectionForm, EditDeviceForm, CommandForm, DeviceImportForm
from .drivers import get_driver, VENDOR_COMMANDS, VENDOR_CHOICES
from .drivers.base import DriverResult
from .topology import discover_topology, get_cached_topology
from . import ocs_helpers
from .cache_utils import cache_delete, cache_get, cache_set
from .keysight_views import _api_auth_required

logger = logging.getLogger(__name__)

# ============================================================
# GLOBAL STATE & CACHING
# ============================================================
_lock = threading.Lock()
_refresh_thread_started = False
REFRESH_INTERVAL = 20
# Shared across gunicorn workers (file cache). OCS REST is slow — keep fresh longer.
_DEVICE_CACHE_PREFIX = 'device_data:v1:'
_DEVICE_CACHE_TTL = 600
_OCS_CACHE_FRESH_SECONDS = 180
_DEFAULT_CACHE_FRESH_SECONDS = REFRESH_INTERVAL * 3


def _get_client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0]
    return request.META.get('REMOTE_ADDR')


def _log_action(user, action, device=None, details='', request=None):
    ip = _get_client_ip(request) if request else None
    ua = ''
    if request:
        ua = (request.META.get('HTTP_USER_AGENT') or '')[:500]
    AuditLog.objects.create(
        user=user, action=action, device=device,
        details=details, ip_address=ip, user_agent=ua,
    )


def _device_cache_key(device_id):
    return f'{_DEVICE_CACHE_PREFIX}{device_id}'


def _parse_cache_timestamp(raw):
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if timezone.is_naive(raw):
            return timezone.make_aware(raw)
        return raw
    if isinstance(raw, str):
        from django.utils.dateparse import parse_datetime
        dt = parse_datetime(raw)
        if dt and timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt
    return None


def _cache_fresh_seconds(vendor_type=None):
    if (vendor_type or '').lower() == 'ocs':
        return _OCS_CACHE_FRESH_SECONDS
    return _DEFAULT_CACHE_FRESH_SECONDS


def _get_cache_entry(device_id):
    entry = cache_get(_device_cache_key(device_id))
    if not isinstance(entry, dict) or 'data' not in entry:
        return None
    ts = _parse_cache_timestamp(entry.get('timestamp'))
    if not ts:
        return None
    return {'data': entry['data'], 'timestamp': ts}


def _get_cached_data(device_id, vendor_type=None):
    entry = _get_cache_entry(device_id)
    if entry:
        age = (timezone.now() - entry['timestamp']).total_seconds()
        if age < _cache_fresh_seconds(vendor_type):
            return entry['data']
    return None


def _get_stale_cached_data(device_id, max_age_seconds=600):
    """Return cached data even if expired, for use when live fetch fails (transient errors)."""
    entry = _get_cache_entry(device_id)
    if entry:
        age = (timezone.now() - entry['timestamp']).total_seconds()
        if age < max_age_seconds:
            return entry['data']
    return None


def _set_cached_data(device_id, data):
    cache_set(
        _device_cache_key(device_id),
        {'data': data, 'timestamp': timezone.now().isoformat()},
        _DEVICE_CACHE_TTL,
    )


def _clear_cached_data(device_id):
    cache_delete(_device_cache_key(device_id))


def _cache_entry_age_seconds(device_id):
    entry = _get_cache_entry(device_id)
    if not entry:
        return None
    return (timezone.now() - entry['timestamp']).total_seconds()


def _should_skip_device_probe(device):
    """Skip redundant TCP probe when we recently confirmed the device is online."""
    if device.status != 'online' or not device.last_seen:
        return False
    return (timezone.now() - device.last_seen).total_seconds() < 300


def _ocs_devices_for_triplet_map():
    return Device.objects.only('id', 'ip_address', 'hostname', 'notes', 'updated_at')


def _schedule_device_data_refresh(device):
    """Refresh device cache in the background (does not block the HTTP response)."""

    def _run():
        try:
            fetch_device_data(device, skip_probe=_should_skip_device_probe(device))
        except Exception:
            logger.exception('Background refresh failed for device %s', device.pk)

    threading.Thread(target=_run, daemon=True).start()


def _build_peer_switch_lldp_for_triplets(triplet_map: dict, ocs_device) -> list:
    """
    For each triplet in triplet_map, look at the peer switch's cached LLDP.
    Returns synthetic LLDP entries: {local_port=triplet, remote_device=..., source='peer_switch_lldp'}
    so that _path_badge / compute_ocs_summary can count verified paths even when the
    OCS itself has no SNMP/REST LLDP agent reporting per-port neighbors.

    Port normalization: site JSON uses "port_17", switch LLDP uses "Ethernet17"/"Eth17"/
    "et-0/0/17" — we strip "port_" and match the trailing integer.
    """
    if not triplet_map:
        return []

    ocs_ip = (ocs_device.ip_address or '').strip()
    ocs_hn = (ocs_device.hostname or '').strip().lower()

    # Group triplets by device_id so we only load each device's cache once
    by_dev: dict = {}
    for triplet, info in triplet_map.items():
        dev_id = info.get('device_id')
        sw_port = info.get('sw_port') or ''
        if not dev_id or not sw_port:
            continue
        by_dev.setdefault(dev_id, {'sw_port_map': {}, 'info': info})
        by_dev[dev_id]['sw_port_map'].setdefault(sw_port, []).append(triplet)

    synthetic = []
    for dev_id, entry in by_dev.items():
        cached = _get_cached_data(dev_id) or _get_stale_cached_data(dev_id)
        if not cached:
            continue  # No cache yet → badge stays "No switch cache"
        peer_lldp = cached.get('lldp_neighbors') or []
        if not peer_lldp:
            continue

        # Build index: last-integer of interface → list of LLDP rows
        port_idx: dict = {}
        for nbr in peer_lldp:
            lp = (nbr.get('local_port') or '').strip()
            if not lp:
                continue
            m = re.search(r'(\d+)(?:[/\-]\d+)*$', lp)
            if m:
                port_idx.setdefault(m.group(1), []).append(nbr)

        for sw_port, triplets in entry['sw_port_map'].items():
            # Normalize sw_port: "port_17" → "17"
            m = re.search(r'(\d+)$', sw_port)
            if not m:
                continue
            port_num = m.group(1)
            nbrs = port_idx.get(port_num) or []
            if not nbrs:
                continue  # Switch has cache but no LLDP on this port

            for triplet in triplets:
                # Pick the first LLDP neighbor for this port
                nbr = nbrs[0]
                rd = (nbr.get('remote_device') or '').strip()
                mgmt = (nbr.get('mgmt_ip') or '').strip()
                chassis_id = (nbr.get('chassis_id') or '').strip()
                is_ocs = (mgmt == ocs_ip or rd.lower() == ocs_hn or
                          (ocs_ip and ocs_ip in rd) or
                          (ocs_hn and ocs_hn in rd.lower()))
                synthetic.append({
                    'local_port': triplet,
                    'remote_device': rd or ocs_ip,
                    'remote_port': nbr.get('remote_port', ''),
                    'chassis_id': chassis_id,
                    'mgmt_ip': mgmt,
                    'system_name': rd,
                    'source': 'peer_switch_lldp',
                    'is_ocs_match': is_ocs,
                    'sw_port': sw_port,
                    'peer_dev_id': dev_id,
                })
    return synthetic


def _build_path_verify_rows(triplet_map: dict, lldp_neighbors: list, ocs_device) -> list:
    """
    Build per-triplet rows for the detailed path verify table.
    Returns list of dicts with: triplet, sw_ip, sw_hostname, sw_port, sw_device_id,
    state, badge_class, badge_text, detail.
    """
    if not triplet_map:
        return []

    # Index existing lldp_neighbors by triplet (local_port)
    lldp_by_triplet: dict = {}
    for n in lldp_neighbors or []:
        lp = ocs_helpers.norm_ocs_triplet_key(n.get('local_port', ''))
        src = n.get('source', '')
        if lp and src != 'ocs_crossconnect' and not (n.get('remote_device') or '').startswith('xconnect:'):
            lldp_by_triplet.setdefault(lp, []).append(n)

    # Check cache existence per device
    dev_cache_exists: dict = {}
    for info in triplet_map.values():
        dev_id = info.get('device_id')
        if dev_id and dev_id not in dev_cache_exists:
            has = bool(_get_cached_data(dev_id) or _get_stale_cached_data(dev_id))
            dev_cache_exists[dev_id] = has

    rows = []
    # Sort by shelf/module/port numeric
    def _sort_key(t):
        parts = [int(x) if x.isdigit() else x for x in t.split('.')]
        return parts

    for triplet in sorted(triplet_map.keys(), key=_sort_key):
        info = triplet_map[triplet]
        sw_ip = info.get('ip') or ''
        sw_hostname = info.get('hostname') or sw_ip
        sw_port = info.get('sw_port') or ''
        sw_device_id = info.get('device_id')

        nbrs = lldp_by_triplet.get(triplet) or []
        has_cache = dev_cache_exists.get(sw_device_id, False)

        if nbrs:
            n = nbrs[0]
            rd = (n.get('remote_device') or '').strip()
            mgmt = (n.get('mgmt_ip') or '').strip()
            chassis = (n.get('chassis_id') or '').strip()
            src = n.get('source', '')
            ocs_ip = (ocs_device.ip_address or '').strip()
            ocs_hn = (ocs_device.hostname or '').strip().lower()
            is_ocs = (mgmt == ocs_ip or rd.lower() == ocs_hn or
                      (ocs_ip and ocs_ip in rd) or (ocs_hn and ocs_hn in rd.lower()))
            is_chassis = bool(chassis) and not is_ocs
            if is_ocs:
                state, badge_class, badge_text = 'ocs_ok', 'bg-success', 'OCS in LLDP'
                detail = f'Peer: {rd or ocs_ip} | src: {src}'
            elif is_chassis:
                state, badge_class, badge_text = 'chassis_ok', 'bg-info text-dark', 'Chassis in LLDP'
                detail = f'Peer: {rd} | chassis_id: {chassis} | src: {src}'
            else:
                state, badge_class, badge_text = 'mismatch', 'bg-warning text-dark', 'Mismatch'
                detail = f'Peer: {rd or "?"} — expected OCS {ocs_ip} | src: {src}'
        elif has_cache:
            state, badge_class, badge_text = 'no_lldp', 'bg-warning text-dark', 'No LLDP on port'
            detail = f'Switch {sw_hostname} refreshed but no LLDP found on {sw_port}'
        else:
            state, badge_class, badge_text = 'no_cache', 'bg-secondary', 'No switch cache'
            detail = f'Refresh {sw_hostname} in LabVault to compare live LLDP to this OCS'

        rows.append({
            'triplet': triplet,
            'sw_ip': sw_ip,
            'sw_hostname': sw_hostname,
            'sw_port': sw_port,
            'sw_device_id': sw_device_id,
            'state': state,
            'badge_class': badge_class,
            'badge_text': badge_text,
            'detail': detail,
        })
    return rows


def _fetch_ocs_device_data(device, driver, data):
    """OCS: shelves grid, ocs_xconns, patch pairs; one crossconnect GET shared by LLDP + table."""
    physical = data.get('physical_data') or []

    # Single REST GET — reused by both get_ocs_crossconnects and get_lldp_neighbors_detail
    raw_rows: list = []
    try:
        raw_rows = driver.fetch_crossconnect_list()
    except Exception as e:
        logger.debug('OCS fetch_crossconnect_list for %s: %s', device.ip_address, e)

    ocs_xc: list = []
    try:
        xr = driver.get_ocs_crossconnects(raw_rows=raw_rows)
        if xr.success and xr.data:
            ocs_xc = list(xr.data)
    except Exception as e:
        logger.debug('OCS get_ocs_crossconnects for %s: %s', device.ip_address, e)

    lldp_neighbors: list = []
    try:
        lldp_result = driver.get_lldp_neighbors_detail(crossconnect_rows=raw_rows)
        if lldp_result.success and lldp_result.data:
            lldp_neighbors = list(lldp_result.data)
    except Exception as e:
        logger.debug('LLDP fetch (OCS) for device %s: %s', device.ip_address, e)
    _merge_lldp_from_keysight_topology(device, lldp_neighbors)
    data['lldp_neighbors'] = _enrich_device_lldp_neighbors(lldp_neighbors)
    all_dev = list(_ocs_devices_for_triplet_map())
    triplet_map = ocs_helpers.build_ocs_triplet_map(device.ip_address, all_dev)

    # Inject peer-switch LLDP: for each triplet in the map, look at the connected
    # switch's cached lldp_neighbors and synthesize entries with local_port=triplet.
    # This lets _path_badge / compute_ocs_summary work even though the OCS itself
    # has no LLDP agents reporting per-port neighbors.
    peer_synthetic = _build_peer_switch_lldp_for_triplets(triplet_map, device)
    if peer_synthetic:
        existing_triplets = {
            ocs_helpers.norm_ocs_triplet_key(n.get('local_port', ''))
            for n in lldp_neighbors
            if n.get('source') != 'ocs_crossconnect'
        }
        for syn in peer_synthetic:
            k = ocs_helpers.norm_ocs_triplet_key(syn.get('local_port', ''))
            if k and k not in existing_triplets:
                lldp_neighbors.append(syn)
                existing_triplets.add(k)

    # Build per-triplet path verification rows for the detailed table in the template
    data['path_verify_rows'] = _build_path_verify_rows(triplet_map, lldp_neighbors, device)
    data['ocs_xconns'] = ocs_helpers.enrich_ocs_xconns(ocs_xc, lldp_neighbors, all_dev, triplet_map)
    # Build shelves AFTER triplet_map is ready so tooltips include switch/chassis info
    shelves, flat = ocs_helpers.build_ocs_shelves(physical, ocs_xc, lldp_neighbors, device.ip_address, triplet_map)
    data['physical_data'] = flat
    data['ocs_shelves'] = shelves
    # Orange overlay lines: live controller cross-connects only (never site-map / planned fabric).
    data['ocs_patch_pairs'] = ocs_helpers.ocs_patch_pairs_for_ui(ocs_xc)
    data['ocs_summary'] = ocs_helpers.compute_ocs_summary(flat, ocs_xc, lldp_neighbors)
    data['is_ocs'] = True
    _set_cached_data(device.id, data)
    return data


def _chassis_lldp_neighbor_matches_device(nbr: dict, device) -> bool:
    """True if a Keysight chassis LLDP row refers to this device (IP/hostname)."""
    ip = (device.ip_address or '').strip()
    if not ip:
        return False
    # Any field contains this IPv4/IPv6 as a token (mgmt often missing; hostname may be unset in LabVault)
    pat = r'(?<![0-9.])' + re.escape(ip) + r'(?![0-9])'
    for val in nbr.values():
        if isinstance(val, str) and re.search(pat, val):
            return True
    mgmt = (nbr.get('mgmt_ip') or '').strip()
    if mgmt == ip:
        return True
    rem = (nbr.get('remote_device') or '').strip().lower()
    if not rem:
        return False
    if rem == ip.lower():
        return True
    hn = (device.hostname or '').strip().lower()
    if hn:
        if rem == hn or rem == hn.split('.')[0]:
            return True
        if hn.startswith(rem + '.'):
            return True
    return False


def _merge_lldp_from_keysight_topology(device, lldp_neighbors: list) -> None:
    """When SONiC reports no LLDP peers, show links learned from chassis / topology DB."""
    seen = {(n.get('local_port'), n.get('remote_device'), n.get('remote_port')) for n in lldp_neighbors}

    try:
        from .models import ChassisDeviceLink
        for cl in ChassisDeviceLink.objects.filter(device=device, link_status='up').select_related('chassis'):
            rdev = cl.chassis.hostname or cl.chassis.ip_address
            tup = (cl.port_device, rdev, cl.port_chassis)
            if tup in seen:
                continue
            seen.add(tup)
            lldp_neighbors.append({
                'local_port': cl.port_device,
                'remote_device': rdev,
                'remote_port': cl.port_chassis,
                'chassis_id': '',
                'mgmt_ip': cl.chassis.mgmt_display,
                'mgmt_ipv4': cl.chassis.ip_address,
            })
    except Exception as e:
        logger.debug('ChassisDeviceLink LLDP merge for %s: %s', device.ip_address, e)

    try:
        from .keysight_views import _get_cached
        from .models import KeysightChassis
        for ch in KeysightChassis.objects.filter(status='online').only('id', 'ip_address', 'hostname'):
            cached = _get_cached(ch.id)
            if not cached:
                continue
            for n in cached.get('lldp_neighbors') or []:
                if not _chassis_lldp_neighbor_matches_device(n, device):
                    continue
                loc = (n.get('remote_port') or n.get('remote_device') or '').strip() or '?'
                rdev = (ch.hostname or ch.ip_address or '').strip() or 'Keysight chassis'
                rport = (n.get('local_port') or '').strip() or '?'
                tup = (loc, rdev, rport)
                if tup in seen:
                    continue
                seen.add(tup)
                lldp_neighbors.append({
                    'local_port': loc,
                    'remote_device': rdev,
                    'remote_port': rport,
                    'chassis_id': '',
                    'mgmt_ip': ch.mgmt_display,
                    'mgmt_ipv4': ch.ip_address,
                })
    except Exception as e:
        logger.debug('Keysight cache LLDP merge for %s: %s', device.ip_address, e)


def _enrich_device_lldp_neighbors(neighbors: list) -> list:
    from .ip_addressing import enrich_lldp_neighbors_for_display
    return enrich_lldp_neighbors_for_display(neighbors or [])


# ============================================================
# DEVICE CONNECTIVITY (via drivers)
# ============================================================

def probe_device(device):
    driver = get_driver(device)
    return driver.probe()


def fetch_device_data(device, skip_probe=False):
    if not skip_probe:
        result = probe_device(device)
    elif _should_skip_device_probe(device):
        result = 'ok'
    else:
        result = probe_device(device)
    if result == 'auth_failed':
        device.status = 'auth_failed'
        device.save(update_fields=['status', 'updated_at'])
        return None
    elif result == 'unreachable':
        device.status = 'offline'
        device.save(update_fields=['status', 'updated_at'])
        return None

    driver = get_driver(device)

    info_result = driver.get_system_info()
    if info_result.success:
        d = info_result.data
        device.hostname = d.get('hostname', device.ip_address)
        device.version = d.get('version', '')
        device.serial_number = d.get('serial_number', '')
        device.model_name = d.get('model_name', '')
        device.mac_address = d.get('mac_address', '')
        device.uptime = d.get('uptime', None)
        device.status = 'online'
        device.last_seen = timezone.now()
        device.save()

    intf_result = driver.get_interfaces()
    if intf_result.success:
        data = intf_result.data
        if device.vendor_type == 'ocs':
            return _fetch_ocs_device_data(device, driver, data)

        # LLDP for device detail — always cache even if port-channel enrichment fails
        lldp_neighbors = []
        try:
            lldp_result = driver.get_lldp_neighbors_detail()
            if lldp_result.success and lldp_result.data:
                lldp_neighbors = list(lldp_result.data)
        except Exception as e:
            logger.debug('LLDP fetch for device %s: %s', device.ip_address, e)
        _merge_lldp_from_keysight_topology(device, lldp_neighbors)
        data['lldp_neighbors'] = _enrich_device_lldp_neighbors(lldp_neighbors)

        # Enrich port-channel data with member links + LLDP peer info
        try:
            pc_members_result = driver.get_port_channel_members()
            pc_members = pc_members_result.data if pc_members_result.success else {}

            # Build LLDP lookup: local_port -> {remote_device, remote_port, ...}
            lldp_map = {}
            for n in lldp_neighbors:
                local = n.get('local_port', '')
                if local:
                    lldp_map[local] = n

            # Build interface status lookup from physical_data (by name AND display_name/alias)
            intf_status_map = {}
            for intf in data.get('physical_data', []):
                intf_status_map[intf.get('name', '')] = intf
                # Also index by display_name and alias so member lookups work
                for alt_key in ('display_name', 'alias'):
                    alt = intf.get(alt_key, '')
                    if alt and alt not in intf_status_map:
                        intf_status_map[alt] = intf

            import re as _re

            def _find_intf(name):
                """Look up interface info with fallback for breakout suffixes.
                e.g. 'Ethernet23/1' -> try 'Ethernet23' if not found."""
                info = intf_status_map.get(name)
                if info:
                    return info
                base = _re.sub(r'/\d+$', '', name)
                if base != name:
                    return intf_status_map.get(base, {})
                return {}

            def _find_lldp(name, info):
                """Look up LLDP info by member name, with breakout and alias fallbacks."""
                for key in (name,
                            info.get('display_name', ''),
                            info.get('alias', ''),
                            _re.sub(r'/\d+$', '', name)):
                    if key and key in lldp_map:
                        return lldp_map[key]
                return {}

            # Enrich each port-channel with its member links
            for pc in data.get('port_channel_data', []):
                pc_name = pc.get('name', '')
                members = pc_members.get(pc_name, [])
                member_details = []
                all_up = True
                for mem_name in members:
                    mem_info = _find_intf(mem_name)
                    mem_status = mem_info.get('status', 'unknown')
                    lldp_info = _find_lldp(mem_name, mem_info)
                    if mem_status not in ('up', 'connected'):
                        all_up = False
                    member_details.append({
                        'name': mem_name,
                        'short_name': mem_info.get('short_name', mem_info.get('display_name', mem_name)),
                        'status': mem_status,
                        'status_color': mem_info.get('status_color', 'off'),
                        'speed_label': mem_info.get('speed_label', ''),
                        'remote_device': lldp_info.get('remote_device', ''),
                        'remote_port': lldp_info.get('remote_port', ''),
                    })
                pc['members'] = member_details
                pc['member_count'] = len(members)
                pc['members_up'] = sum(1 for m in member_details if m['status'] in ('up', 'connected'))
                pc['all_up'] = all_up and len(members) > 0
                # Also check LLDP on port-channel itself
                pc_lldp = lldp_map.get(pc_name, {})
                pc['remote_device'] = pc_lldp.get('remote_device', '')
                pc['remote_port'] = pc_lldp.get('remote_port', '')
        except Exception as e:
            logger.debug(f"Error enriching port-channel data: {e}")

        _set_cached_data(device.id, data)
        if device.vendor_type == "arista":
            try:
                from .drivers.arista import AristaDriver
                raw_ak = (getattr(device, "api_key", None) or "").strip()
                opts: dict = {}
                if raw_ak.startswith("{"):
                    try:
                        o = json.loads(raw_ak)
                        if isinstance(o, dict):
                            opts = o
                    except json.JSONDecodeError:
                        pass
                if opts.get("auto_enable_eapi") and isinstance(driver, AristaDriver):
                    pr = driver.promote_to_eapi()
                    if not pr.success:
                        logger.warning(
                            "Arista auto_enable_eapi device %s: %s",
                            device.id,
                            pr.error,
                        )
            except Exception as e:
                logger.debug("Arista auto_enable_eapi: %s", e)
        return data

    return None


def fetch_device_health(device):
    driver = get_driver(device)
    result = driver.get_health()
    if result.success:
        data = result.data
        DeviceSnapshot.objects.create(
            device=device,
            cpu_utilization=data.get('cpu_utilization', 0),
            memory_used=data.get('memory_used', 0),
            memory_total=data.get('memory_total', 0),
            temperature=data.get('temperature'),
            uptime=data.get('uptime', 0),
        )
        _check_health_thresholds(device, data)
        return data
    return None


def _check_health_thresholds(device, health_data):
    cpu = health_data.get('cpu_utilization', 0)
    mem = health_data.get('memory_percent', 0)
    if cpu and cpu > device.cpu_threshold:
        if not Alert.objects.filter(device=device, alert_type='high_cpu',
                                     created_at__gte=timezone.now() - timedelta(minutes=5)).exists():
            Alert.objects.create(
                device=device, alert_type='high_cpu', severity='warning',
                message=f"CPU at {cpu}% (threshold: {device.cpu_threshold}%)")
            _send_webhooks(device, 'high_cpu', 'warning',
                          f"CPU at {cpu}% on {device.hostname or device.ip_address}")
    if mem and mem > device.memory_threshold:
        if not Alert.objects.filter(device=device, alert_type='high_memory',
                                     created_at__gte=timezone.now() - timedelta(minutes=5)).exists():
            Alert.objects.create(
                device=device, alert_type='high_memory', severity='warning',
                message=f"Memory at {mem}% (threshold: {device.memory_threshold}%)")
            _send_webhooks(device, 'high_memory', 'warning',
                          f"Memory at {mem}% on {device.hostname or device.ip_address}")


# ============================================================
# WEBHOOK NOTIFICATIONS
# ============================================================

def _send_webhooks(device, alert_type, severity, message):
    """Send alert notifications to configured webhook endpoints."""
    endpoints = WebhookEndpoint.objects.filter(enabled=True)
    for endpoint in endpoints:
        if endpoint.severity_filter:
            allowed = [s.strip() for s in endpoint.severity_filter.split(',')]
            if severity not in allowed:
                continue
        try:
            payload = _build_webhook_payload(endpoint.webhook_type, device, alert_type, severity, message)
            requests.post(endpoint.url, json=payload, timeout=5,
                         headers={'Content-Type': 'application/json'})
            endpoint.last_triggered = timezone.now()
            endpoint.save(update_fields=['last_triggered'])
        except Exception as e:
            logger.debug(f"Webhook send error to {endpoint.name}: {e}")


def _build_webhook_payload(webhook_type, device, alert_type, severity, message):
    if webhook_type == 'slack':
        color = {'critical': '#FF0000', 'warning': '#FFA500', 'info': '#2196F3'}.get(severity, '#607D8B')
        return {
            'attachments': [{
                'color': color,
                'title': f"LabVault Alert: {alert_type}",
                'text': message,
                'fields': [
                    {'title': 'Device', 'value': str(device), 'short': True},
                    {'title': 'Severity', 'value': severity.upper(), 'short': True},
                ],
                'ts': int(timezone.now().timestamp()),
            }]
        }
    elif webhook_type == 'teams':
        return {
            '@type': 'MessageCard',
            'themeColor': {'critical': 'FF0000', 'warning': 'FFA500'}.get(severity, '2196F3'),
            'summary': f"LabVault: {alert_type}",
            'sections': [{
                'activityTitle': f"LabVault Alert: {alert_type}",
                'facts': [
                    {'name': 'Device', 'value': str(device)},
                    {'name': 'Severity', 'value': severity.upper()},
                    {'name': 'Message', 'value': message},
                ],
            }]
        }
    else:  # generic / pagerduty
        return {
            'source': 'netconnect',
            'device': str(device),
            'device_ip': device.ip_address,
            'alert_type': alert_type,
            'severity': severity,
            'message': message,
            'timestamp': timezone.now().isoformat(),
        }


# ============================================================
# BACKGROUND REFRESH (20-second interval)
# ============================================================

def _refresh_all_devices():
    while True:
        try:
            close_old_connections()
            devices = list(Device.objects.filter(maintenance_mode=False))
            with ThreadPoolExecutor(max_workers=min(10, max(1, len(devices)))) as pool:
                futures = {
                    pool.submit(_refresh_single_device_safe, d): d for d in devices
                }
                for future in as_completed(futures, timeout=45):
                    try:
                        future.result()
                    except Exception as e:
                        dev = futures[future]
                        logger.error(f"Refresh error for {dev.ip_address}: {e}")
        except Exception as e:
            close_old_connections()
            logger.error(f"Refresh thread error: {e}")
            from connect.worker_status import record_device_refresh
            record_device_refresh(error=str(e))
        finally:
            close_old_connections()
            from connect.worker_status import record_device_refresh
            record_device_refresh()
        time.sleep(REFRESH_INTERVAL)


def _refresh_single_device_safe(device):
    close_old_connections()
    try:
        return _refresh_single_device(device)
    finally:
        close_old_connections()


def _refresh_single_device(device):
    try:
        old_status = device.status
        fetch_device_data(device)
        device.refresh_from_db()
        if old_status == 'online' and device.status == 'offline':
            Alert.objects.create(
                device=device, alert_type='device_down', severity='critical',
                message=f"{device.hostname or device.ip_address} went offline")
            _send_webhooks(device, 'device_down', 'critical',
                          f"{device.hostname or device.ip_address} went offline")
        elif old_status != 'online' and device.status == 'online':
            Alert.objects.create(
                device=device, alert_type='device_up', severity='info',
                message=f"{device.hostname or device.ip_address} came online")
        try:
            from .changelog import log_status_transition
            log_status_transition(
                'device',
                device.id,
                str(device.hostname or device.ip_address or device.pk),
                old_status,
                device.status,
            )
        except Exception:
            logger.exception('changelog status for device %s', device.pk)
    except Exception as e:
        logger.error(f"Error refreshing {device.ip_address}: {e}")


def _inprocess_refresh_disabled() -> bool:
    flag = (os.environ.get('LABVAULT_DISABLE_INPROCESS_REFRESH') or '').strip().lower()
    if flag in ('1', 'true', 'yes', 'on'):
        return True
    return 'gunicorn' in sys.modules


def start_refresh_thread():
    global _refresh_thread_started
    if _inprocess_refresh_disabled():
        return
    if not _refresh_thread_started:
        _refresh_thread_started = True
        t = threading.Thread(target=_refresh_all_devices, daemon=True)
        t.start()
        logger.info(f"Device refresh thread started (interval: {REFRESH_INTERVAL}s)")


# ============================================================
# AUTH VIEWS
# ============================================================

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = authenticate(username=form.cleaned_data['username'],
                              password=form.cleaned_data['password'])
            if user is not None:
                login(request, user)
                _log_action(user, 'login', request=request)
                messages.success(request, f'Welcome back, {user.username}!')
                return redirect('dashboard')
    else:
        form = AuthenticationForm()
    from django.conf import settings as _settings
    ldap_enabled = bool(getattr(_settings, 'AUTH_LDAP_SERVER_URI', ''))
    return render(request, 'connect/login.html', {
        'form': form,
        'ldap_enabled': ldap_enabled,
    })


def logout_view(request):
    if request.user.is_authenticated:
        _log_action(request.user, 'logout', request=request)
    logout(request)
    messages.info(request, 'You have been logged out.')
    return redirect('login')


# ============================================================
# DASHBOARD
# ============================================================

@login_required
def dashboard(request):
    devices = Device.objects.all()
    start_refresh_thread()

    vendor_filter = request.GET.get('vendor', '')
    status_filter = request.GET.get('status', '')
    group_filter = request.GET.get('group', '')
    site_filter = request.GET.get('site', '')
    search_q = request.GET.get('q', '')
    tag_filter = request.GET.get('tag', '')

    filtered = devices
    if vendor_filter:
        filtered = filtered.filter(vendor_type=vendor_filter)
    if status_filter:
        filtered = filtered.filter(status=status_filter)
    if group_filter:
        filtered = filtered.filter(group_name=group_filter)
    if site_filter:
        filtered = filtered.filter(site=site_filter)
    if search_q:
        filtered = filtered.filter(
            Q(ip_address__icontains=search_q) | Q(hostname__icontains=search_q) |
            Q(model_name__icontains=search_q) | Q(serial_number__icontains=search_q) |
            Q(tags__icontains=search_q) | Q(notes__icontains=search_q))
    if tag_filter:
        filtered = filtered.filter(tags__icontains=tag_filter)

    device_stats = Device.objects.aggregate(
        total=Count('id'),
        online=Count('id', filter=Q(status='online')),
        offline=Count('id', filter=Q(status='offline')),
        auth_failed=Count('id', filter=Q(status='auth_failed')),
    )
    vendor_stats = devices.values('vendor_type').annotate(count=Count('id')).order_by('vendor_type')
    recent_alerts = Alert.objects.filter(acknowledged=False)[:10]
    all_groups = devices.exclude(group_name='').values_list('group_name', flat=True).distinct()
    all_sites = devices.exclude(site='').values_list('site', flat=True).distinct()
    all_tags = set()
    for tags_raw in devices.exclude(tags='').values_list('tags', flat=True):
        all_tags.update(t.strip() for t in tags_raw.split(',') if t.strip())

    context = {
        'devices': filtered, 'total_devices': device_stats['total'],
        'online_devices': device_stats['online'], 'offline_devices': device_stats['offline'],
        'auth_failed_devices': device_stats['auth_failed'], 'vendor_stats': list(vendor_stats),
        'recent_alerts': recent_alerts, 'all_groups': sorted(all_groups),
        'all_sites': sorted(all_sites), 'all_tags': sorted(all_tags),
        'vendor_filter': vendor_filter, 'status_filter': status_filter,
        'group_filter': group_filter, 'site_filter': site_filter,
        'search_q': search_q, 'tag_filter': tag_filter,
        'refresh_interval': REFRESH_INTERVAL,
    }
    return render(request, 'connect/dashboard.html', context)


# ============================================================
# DEVICE DETAIL - FAST (serves from cache)
# ============================================================

@login_required
def device_detail(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    force_refresh = request.GET.get('refresh') == '1'
    vendor = (device.vendor_type or '').lower()
    context = {'device': device}
    stale_data_warning = False

    cached = None if force_refresh else _get_cached_data(device_id, vendor_type=vendor)
    if cached:
        context.update(cached)
    else:
        stale = None if force_refresh else _get_stale_cached_data(device_id)
        if stale:
            context.update(stale)
            age = _cache_entry_age_seconds(device_id)
            if device.status in ('online', 'unknown') and (age or 0) > REFRESH_INTERVAL:
                _schedule_device_data_refresh(device)
        elif device.status in ('online', 'unknown'):
            skip = _should_skip_device_probe(device)
            device_data = fetch_device_data(device, skip_probe=skip)
            if not device_data:
                device_data = fetch_device_data(device, skip_probe=True)
            if device_data:
                context.update(device_data)
            elif stale := _get_stale_cached_data(device_id):
                context.update(stale)
                stale_data_warning = True
            else:
                context['error'] = "Unable to connect. Check credentials and connectivity."
        else:
            if device.status == 'auth_failed':
                context['error'] = "Authentication failed. Fix credentials."
            elif device.status == 'offline':
                context['error'] = "Device is offline."

    context['stale_data_warning'] = stale_data_warning

    context['vendor_commands'] = VENDOR_COMMANDS.get(device.vendor_type, [])
    context.setdefault('is_ocs', False)
    if context.get('is_ocs'):
        context.setdefault('ocs_patch_pairs', [])
        context.setdefault('ocs_shelves', [])
        context.setdefault('ocs_xconns', [])
        context.setdefault('path_verify_rows', [])
        context.setdefault('ocs_summary', {
            'active_xconns': 0,
            'ocs_ports': 0,
            'paths_up': 0,
            'lldp_path_ok': 0,
            'lldp_path_total': 0,
            'chassis_in_lldp': 0,
        })
    return render(request, 'connect/device_detail.html', context)


# ============================================================
# HEALTH (AJAX)
# ============================================================

@login_required
def device_health(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    health = fetch_device_health(device)
    if health:
        return JsonResponse({'status': 'ok', 'data': health})
    return JsonResponse({'status': 'error', 'message': 'Unable to fetch health.'})


@login_required
def device_health_history(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    hours = int(request.GET.get('hours', 24))
    since = timezone.now() - timedelta(hours=hours)
    snapshots = DeviceSnapshot.objects.filter(device=device, timestamp__gte=since).order_by('timestamp')
    data = [{'timestamp': s.timestamp.isoformat(), 'cpu': s.cpu_utilization,
             'memory_percent': s.memory_percent, 'temperature': s.temperature,
             'uptime': s.uptime} for s in snapshots]
    return JsonResponse({'status': 'ok', 'data': data})


# ============================================================
# BGP / OSPF / ENVIRONMENT (NEW)
# ============================================================

@login_required
def device_bgp(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_bgp_summary()
    return render(request, 'connect/device_bgp.html', {
        'device': device, 'devices': Device.objects.all(),
        'peers': result.data if result.success else [],
    })


@login_required
def device_ospf(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_ospf_neighbors()
    return render(request, 'connect/device_ospf.html', {
        'device': device, 'devices': Device.objects.all(),
        'neighbors': result.data if result.success else [],
    })


@login_required
def device_environment(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_environment()
    data = result.data if result.success else {'sensors': [], 'fans': [], 'power_supplies': []}
    return render(request, 'connect/device_environment.html', {
        'device': device, 'devices': Device.objects.all(),
        'sensors': data.get('sensors', []),
        'fans': data.get('fans', []),
        'power_supplies': data.get('power_supplies', []),
    })


@login_required
def device_bgp_json(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_bgp_summary()
    return JsonResponse({'status': 'ok', 'data': result.data if result.success else []})


@login_required
def device_ospf_json(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_ospf_neighbors()
    return JsonResponse({'status': 'ok', 'data': result.data if result.success else []})


@login_required
def device_environment_json(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_environment()
    return JsonResponse({'status': 'ok', 'data': result.data if result.success else {}})


# ============================================================
# INTERFACE COUNTERS / TRENDING (NEW)
# ============================================================

@login_required
def device_counters_json(request, device_id):
    """Fetch live interface counters and store snapshot."""
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_interface_counters()
    if result.success and result.data:
        for c in result.data:
            InterfaceSnapshot.objects.create(
                device=device,
                interface_name=c.get('name', ''),
                bandwidth_in=c.get('bytes_in', 0),
                bandwidth_out=c.get('bytes_out', 0),
                packets_in=c.get('packets_in', 0),
                packets_out=c.get('packets_out', 0),
                errors_in=c.get('errors_in', 0),
                errors_out=c.get('errors_out', 0),
            )
        return JsonResponse({'status': 'ok', 'data': result.data})
    return JsonResponse({'status': 'error', 'data': []})


@login_required
def device_interface_trending(request, device_id):
    """Get interface utilization history for sparklines."""
    device = get_object_or_404(Device, id=device_id)
    intf_name = request.GET.get('interface', '')
    hours = int(request.GET.get('hours', 24))
    since = timezone.now() - timedelta(hours=hours)

    snapshots = InterfaceSnapshot.objects.filter(
        device=device, timestamp__gte=since
    )
    if intf_name:
        snapshots = snapshots.filter(interface_name=intf_name)

    data = [{'timestamp': s.timestamp.isoformat(), 'interface': s.interface_name,
             'bytes_in': s.bandwidth_in, 'bytes_out': s.bandwidth_out,
             'errors_in': s.errors_in, 'errors_out': s.errors_out
             } for s in snapshots.order_by('timestamp')[:500]]
    return JsonResponse({'status': 'ok', 'data': data})


# ============================================================
# DOM / TRANSCEIVER (NEW)
# ============================================================

@login_required
def device_dom(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_dom_info()
    return render(request, 'connect/device_dom.html', {
        'device': device, 'devices': Device.objects.all(),
        'transceivers': result.data if result.success else [],
    })


# ============================================================
# ROUTING / VLANs / CONFIG / TERMINAL
# ============================================================

@login_required
def device_routing(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_routes()
    return render(request, 'connect/device_routing.html', {
        'device': device, 'devices': Device.objects.all(),
        'routes': result.data if result.success else [],
    })


@login_required
def device_vlans(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_vlans()
    return render(request, 'connect/device_vlans.html', {
        'device': device, 'devices': Device.objects.all(),
        'vlans': result.data if result.success else [],
    })


@login_required
def device_config(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    try:
        running_result = driver.get_running_config()
    except NotImplementedError:
        running_result = DriverResult(success=False, error='Not supported')
    try:
        startup_result = driver.get_startup_config()
    except NotImplementedError:
        startup_result = DriverResult(success=False, error='Not supported')
    running = running_result.data if running_result.success else ''
    startup = startup_result.data if startup_result.success else ''
    diff_lines = list(difflib.unified_diff(
        startup.splitlines(keepends=True), running.splitlines(keepends=True),
        fromfile='startup-config', tofile='running-config', lineterm=''))
    backups = ConfigBackup.objects.filter(device=device)[:20]
    return render(request, 'connect/device_config.html', {
        'device': device, 'devices': Device.objects.all(),
        'running_config': running, 'startup_config': startup,
        'diff_text': '\n'.join(diff_lines), 'has_diff': len(diff_lines) > 0,
        'backups': backups,
    })


@login_required
@require_POST
def backup_config(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_running_config()
    if result.success:
        last = ConfigBackup.objects.filter(device=device, config_type='running').first()
        diff = ''
        if last:
            diff = '\n'.join(difflib.unified_diff(
                last.content.splitlines(), result.data.splitlines(),
                fromfile='previous', tofile='current', lineterm=''))
        ConfigBackup.objects.create(device=device, config_type='running',
                                   content=result.data, diff_from_previous=diff,
                                   created_by=request.user)
        _log_action(request.user, 'config_backup', device=device, request=request)
        messages.success(request, 'Config backup saved!')
    else:
        messages.error(request, f'Backup failed: {result.error}')
    return redirect('device_config', device_id=device_id)


def device_terminal(request, device_id):
    """Removed from the customer SKU — free-form device shell is not shipped."""
    from django.http import HttpResponseNotFound
    return HttpResponseNotFound("device terminal is not included on this SKU")


# ============================================================
# ARP / MAC / LLDP
# ============================================================

@login_required
def device_arp(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_arp_table()
    return render(request, 'connect/device_arp.html', {
        'device': device, 'devices': Device.objects.all(),
        'entries': result.data if result.success else [],
    })


@login_required
def device_mac(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_mac_table()
    return render(request, 'connect/device_mac.html', {
        'device': device, 'devices': Device.objects.all(),
        'entries': result.data if result.success else [],
    })


@login_required
def device_lldp(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    neighbors = []
    error = None
    try:
        driver = get_driver(device)
        result = driver.get_lldp_neighbors()
        if result.success:
            neighbors = result.data or []
        else:
            error = getattr(result, 'error', None) or 'LLDP query failed'
    except Exception as exc:
        logger.exception('device_lldp failed for %s: %s', device.ip_address, exc)
        error = str(exc)
    return render(request, 'connect/device_lldp.html', {
        'device': device, 'devices': Device.objects.all(),
        'neighbors': neighbors,
        'error': error,
    })


# ============================================================
# FIREWALL-SPECIFIC (policies, vpn)
# ============================================================

@login_required
def device_policies(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_security_policies()
    return render(request, 'connect/device_policies.html', {
        'device': device, 'devices': Device.objects.all(),
        'policies': result.data if result.success else [],
        'is_firewall': device.is_firewall,
    })


@login_required
def device_vpn(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.get_vpn_tunnels()
    return render(request, 'connect/device_vpn.html', {
        'device': device, 'devices': Device.objects.all(),
        'tunnels': result.data if result.success else [],
    })


# ============================================================
# DEVICE CRUD
# ============================================================

@login_required
def add_device(request):
    if request.method == 'POST':
        form = ConnectionForm(request.POST)
        if form.is_valid():
            ip = form.cleaned_data['ip_address']
            if Device.objects.filter(ip_address=ip).exists():
                form.add_error('ip_address', 'Device with this IP already exists.')
            else:
                device = form.save()
                _log_action(request.user, 'device_add', device=device,
                           details=f"Added {device.vendor_type} device {ip}", request=request)
                messages.success(request, f'Device {ip} added!')
                return redirect('dashboard')
    else:
        form = ConnectionForm()
    return render(request, 'connect/add_device.html', {
        'form': form, 'devices': Device.objects.all(),
    })


@login_required
def edit_device(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method == 'POST':
        form = EditDeviceForm(request.POST, instance=device)
        if form.is_valid():
            updated = form.save(commit=False)
            updated.status = 'unknown'
            updated.transport = 'auto'
            from .drivers.arista import clear_cache as arista_clear
            from .drivers.sonic import clear_cache as sonic_clear
            from .drivers.fortigate import clear_cache as forti_clear
            from .drivers.paloalto import clear_cache as pa_clear
            for clear_fn in (arista_clear, sonic_clear, forti_clear, pa_clear):
                clear_fn(device.ip_address)
                clear_fn(updated.ip_address)
            _clear_cached_data(device.id)
            updated.save()
            _log_action(request.user, 'device_edit', device=updated,
                       details=f"Edited device {updated.ip_address}", request=request)
            messages.success(request, f'Device {updated.ip_address} updated!')
            return redirect('device_detail', device_id=device.id)
    else:
        form = EditDeviceForm(instance=device)
    return render(request, 'connect/edit_device.html', {
        'form': form, 'device': device, 'devices': Device.objects.all(),
    })


@login_required
@require_POST
def delete_device(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    ip = device.ip_address
    _clear_cached_data(device.id)
    _log_action(request.user, 'device_delete', details=f"Deleted {ip}", request=request)
    device.delete()
    messages.success(request, f'Device {ip} deleted.')
    return redirect('dashboard')


# ============================================================
# BULK OPERATIONS
# ============================================================

@login_required
@require_POST
def bulk_action(request):
    action = request.POST.get('bulk_action', '')
    device_ids = request.POST.getlist('device_ids')
    if not device_ids:
        messages.warning(request, 'No devices selected.')
        return redirect('dashboard')

    devices_qs = Device.objects.filter(id__in=device_ids)

    if action == 'delete':
        count = devices_qs.count()
        devices_qs.delete()
        _log_action(request.user, 'bulk_action',
                   details=f"Bulk deleted {count} devices", request=request)
        messages.success(request, f'Deleted {count} devices.')
    elif action == 'refresh':
        for d in devices_qs:
            fetch_device_data(d)
        messages.success(request, f'Refreshed {devices_qs.count()} devices.')
    elif action == 'maintenance_on':
        devices_qs.update(maintenance_mode=True, status='maintenance')
        messages.success(request, f'Maintenance ON for {devices_qs.count()} devices.')
    elif action == 'maintenance_off':
        devices_qs.update(maintenance_mode=False, status='unknown')
        messages.success(request, f'Maintenance OFF for {devices_qs.count()} devices.')
    elif action == 'backup_config':
        backed_up = 0
        for d in devices_qs:
            driver = get_driver(d)
            result = driver.get_running_config()
            if result.success:
                ConfigBackup.objects.create(device=d, config_type='running',
                                          content=result.data, created_by=request.user)
                backed_up += 1
        messages.success(request, f'Backed up {backed_up} devices.')
    elif action == 'enable_lldp':
        enabled = 0
        failed = []
        for d in devices_qs:
            if d.status != 'online':
                continue
            driver = get_driver(d)
            result = driver.enable_lldp()
            if result.success:
                enabled += 1
                _log_action(request.user, 'config_change',
                           details=f"LLDP enabled on {d.hostname or d.ip_address}: {result.data.get('enabled_count', 0)} interfaces",
                           device=d, request=request)
            else:
                failed.append(f'{d.ip_address}: {result.error}')
        msg = f'LLDP enabled on {enabled} device(s).'
        if failed:
            msg += f' Failed: {"; ".join(failed[:3])}'
        messages.success(request, msg)
    return redirect('dashboard')


# ============================================================
# ALERTS
# ============================================================

@login_required
def alerts_view(request):
    alerts = Alert.objects.select_related('device', 'acknowledged_by').all()
    severity = request.GET.get('severity', '')
    ack_filter = request.GET.get('ack', '')
    if severity:
        alerts = alerts.filter(severity=severity)
    if ack_filter == 'unack':
        alerts = alerts.filter(acknowledged=False)
    elif ack_filter == 'ack':
        alerts = alerts.filter(acknowledged=True)
    return render(request, 'connect/alerts.html', {
        'alerts': alerts[:200], 'devices': Device.objects.all(),
        'severity_filter': severity, 'ack_filter': ack_filter,
    })


@login_required
@require_POST
def acknowledge_alert(request, alert_id):
    alert = get_object_or_404(Alert, id=alert_id)
    alert.acknowledged = True
    alert.acknowledged_by = request.user
    alert.acknowledged_at = timezone.now()
    alert.save()
    return JsonResponse({'status': 'ok'})


@login_required
@require_POST
def acknowledge_all_alerts(request):
    Alert.objects.filter(acknowledged=False).update(
        acknowledged=True, acknowledged_by=request.user, acknowledged_at=timezone.now())
    messages.success(request, 'All alerts acknowledged.')
    return redirect('alerts')


# ============================================================
# COMPLIANCE
# ============================================================

@login_required
def compliance_view(request):
    rules = ComplianceRule.objects.all()
    results = ComplianceResult.objects.select_related('device', 'rule').all()[:200]
    return render(request, 'connect/compliance.html', {
        'rules': rules, 'results': results, 'devices': Device.objects.all(),
    })


@login_required
@require_POST
def run_compliance_check(request):
    device_ids = request.POST.getlist('device_ids')
    devices_qs = Device.objects.filter(id__in=device_ids) if device_ids else Device.objects.filter(status='online')
    rules = ComplianceRule.objects.filter(enabled=True)
    checked, passed, failed = 0, 0, 0
    for device in devices_qs:
        driver = get_driver(device)
        config_result = driver.get_running_config()
        if not config_result.success:
            continue
        config = config_result.data
        for rule in rules.filter(Q(vendor_type=device.vendor_type) | Q(vendor_type='')):
            match = bool(re.search(rule.pattern, config, re.MULTILINE))
            is_pass = match == rule.should_exist
            ComplianceResult.objects.create(device=device, rule=rule, passed=is_pass,
                                          details=f"Pattern {'found' if match else 'not found'}")
            checked += 1
            passed += 1 if is_pass else 0
            failed += 0 if is_pass else 1
    _log_action(request.user, 'compliance_check',
               details=f"{checked} checks: {passed} passed, {failed} failed", request=request)
    messages.success(request, f'Compliance: {checked} checks, {passed} passed, {failed} failed.')
    return redirect('compliance')


# ============================================================
# CONFIG SEARCH ACROSS FLEET (NEW)
# ============================================================

@login_required
def config_search(request):
    """Search for a pattern across all device configs."""
    pattern = request.GET.get('pattern', '')
    results = []
    if pattern:
        devices_qs = Device.objects.filter(status='online')
        for device in devices_qs:
            # Search in most recent backup first (fast)
            backup = ConfigBackup.objects.filter(device=device, config_type='running').first()
            if backup and backup.content:
                matches = re.findall(f'.*{re.escape(pattern)}.*', backup.content, re.IGNORECASE)
                if matches:
                    results.append({
                        'device': device,
                        'matches': matches[:10],
                        'match_count': len(matches),
                    })
    return render(request, 'connect/config_search.html', {
        'devices': Device.objects.all(),
        'pattern': pattern,
        'results': results,
    })


# ============================================================
# CONFIG DIFF TIMELINE (NEW)
# ============================================================

@login_required
def config_timeline(request, device_id):
    """Show config backup history with diffs."""
    device = get_object_or_404(Device, id=device_id)
    backups = ConfigBackup.objects.filter(device=device).order_by('-created_at')[:50]
    return render(request, 'connect/config_timeline.html', {
        'device': device, 'devices': Device.objects.all(),
        'backups': backups,
    })


@login_required
def config_diff_view(request, backup_id):
    """View diff between two backups."""
    backup = get_object_or_404(ConfigBackup, id=backup_id)
    previous = ConfigBackup.objects.filter(
        device=backup.device, config_type=backup.config_type,
        created_at__lt=backup.created_at
    ).first()

    if previous:
        diff_lines = list(difflib.unified_diff(
            previous.content.splitlines(keepends=True),
            backup.content.splitlines(keepends=True),
            fromfile=f'Backup {previous.created_at.strftime("%Y-%m-%d %H:%M")}',
            tofile=f'Backup {backup.created_at.strftime("%Y-%m-%d %H:%M")}',
            lineterm=''))
        diff_text = '\n'.join(diff_lines)
    else:
        diff_text = 'No previous backup to compare.'

    return JsonResponse({
        'diff': diff_text,
        'date': backup.created_at.isoformat(),
        'device': str(backup.device),
    })


# ============================================================
# SAVED COMMANDS
# ============================================================

@login_required
@require_POST
def save_command(request):
    name = request.POST.get('name', '')
    command = request.POST.get('command', '')
    vendor = request.POST.get('vendor_type', '')
    desc = request.POST.get('description', '')
    if name and command:
        SavedCommand.objects.create(user=request.user, name=name, command=command,
                                   vendor_type=vendor, description=desc)
        messages.success(request, f'Command "{name}" saved!')
    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))


@login_required
@require_POST
def delete_saved_command(request, cmd_id):
    cmd = get_object_or_404(SavedCommand, id=cmd_id, user=request.user)
    cmd.delete()
    return JsonResponse({'status': 'ok'})


# ============================================================
# IMPORT / EXPORT
# ============================================================

@login_required
def export_labvault_dataset(request):
    """Full JSON export UI and download (staff only)."""
    if not (request.user.is_staff or request.user.is_superuser):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied

    if request.GET.get('download'):
        import json
        from .labvault_dataset import build_export_payload, _json_default
        try:
            payload = build_export_payload()
        except Exception as exc:
            logger.exception('LabVault full export failed')
            messages.error(request, f'Export failed: {exc}')
            return redirect('export_labvault_dataset')
        stamp = timezone.now().strftime('%Y%m%d-%H%M%S')
        response = HttpResponse(
            json.dumps(payload, indent=2, default=_json_default),
            content_type='application/json',
        )
        response['Content-Disposition'] = f'attachment; filename="labvault-export-{stamp}.json"'
        return response

    return render(request, 'connect/export_labvault.html')


@login_required
def export_labvault_bundle(request):
    """Multibundle tarball export (inventory + topologies + site schemas + optional cache)."""
    if not (request.user.is_staff or request.user.is_superuser):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied

    if not request.GET.get('download'):
        return render(request, 'connect/export_labvault.html')

    import tempfile
    from pathlib import Path

    from .labvault_bundle import write_bundle

    lab = (request.GET.get('lab') or '').strip()
    ip_prefix = (request.GET.get('ip_prefix') or '').strip()
    topology_ids = []
    for raw in request.GET.getlist('topology_id'):
        try:
            topology_ids.append(int(raw))
        except (TypeError, ValueError):
            pass
    topology_names = [n.strip() for n in request.GET.getlist('topology_name') if n.strip()]
    # No baked lab name. Without an explicit topology filter, export matching inventory.

    try:
        with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
            tmp_path = Path(tmp.name)
        write_bundle(
            tmp_path,
            lab=lab,
            ip_prefix=ip_prefix,
            topology_ids=topology_ids or None,
            topology_names=topology_names or None,
            include_databases=request.GET.get('include_databases') == '1',
            include_lldp_cache=request.GET.get('no_lldp_cache') != '1',
            include_secrets=request.GET.get('include_secrets') == '1',
            include_capex=request.GET.get('include_capex') == '1',
        )
        data = tmp_path.read_bytes()
        tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.exception('LabVault multibundle export failed')
        messages.error(request, f'Bundle export failed: {exc}')
        return redirect('export_labvault_dataset')

    stamp = timezone.now().strftime('%Y%m%d-%H%M%S')
    slug = re.sub(r'[^a-z0-9]+', '-', (lab or 'full').lower()).strip('-') or 'full'
    response = HttpResponse(data, content_type='application/gzip')
    response['Content-Disposition'] = (
        f'attachment; filename="labvault-bundle-{slug}-{stamp}.tar.gz"'
    )
    return response


@login_required
def export_devices(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="network_devices.csv"'
    writer = csv.writer(response)
    writer.writerow(['IP Address', 'Username', 'Password', 'Vendor', 'Hostname',
                     'Version', 'Serial Number', 'Model', 'Status', 'Site',
                     'Group', 'Tags', 'Notes', 'API Port', 'API Key'])
    for d in Device.objects.all():
        writer.writerow([d.ip_address, d.username, d.password, d.vendor_type,
                        d.hostname or '', d.version or '', d.serial_number or '',
                        d.model_name or '', d.status, d.site, d.group_name,
                        d.tags, d.notes, d.api_port or '', d.api_key])
    return response


@login_required
def import_devices(request):
    if not (request.user.is_staff or request.user.is_superuser):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied

    if request.method == 'POST':
        bundle_file = request.FILES.get('bundle_file')
        if bundle_file:
            import tempfile
            from pathlib import Path

            from .labvault_bundle import import_bundle

            try:
                with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
                    for chunk in bundle_file.chunks():
                        tmp.write(chunk)
                    tmp_path = Path(tmp.name)
                stats = import_bundle(
                    tmp_path,
                    import_databases=request.POST.get('import_databases') == '1',
                    import_lldp_cache=request.POST.get('no_lldp_cache') != '1',
                    import_secrets=request.POST.get('import_secrets') == '1',
                )
                tmp_path.unlink(missing_ok=True)
            except Exception as exc:
                messages.error(request, f'Bundle import failed: {exc}')
                return redirect('import_devices')
            _log_action(
                request.user, 'import',
                details=f'LabVault bundle import: {stats}', request=request,
            )
            messages.success(request, f'LabVault bundle imported: {stats}')
            return redirect('dashboard')

        full_file = request.FILES.get('labvault_file')
        if full_file:
            import json
            from .labvault_dataset import import_from_payload
            try:
                payload = json.loads(full_file.read().decode('utf-8'))
                stats = import_from_payload(payload)
            except Exception as exc:
                messages.error(request, f'LabVault import failed: {exc}')
                return redirect('import_devices')
            _log_action(
                request.user, 'import',
                details=f'LabVault full import: {stats}', request=request,
            )
            messages.success(request, f'LabVault dataset imported: {stats}')
            return redirect('dashboard')

        form = DeviceImportForm(request.POST, request.FILES)
        if form.is_valid():
            csv_file = request.FILES['csv_file']
            decoded = csv_file.read().decode('utf-8')
            reader = csv.DictReader(io.StringIO(decoded))
            count = 0
            for row in reader:
                ip = row.get('IP Address', '').strip()
                if ip and not Device.objects.filter(ip_address=ip).exists():
                    Device.objects.create(
                        ip_address=ip,
                        username=row.get('Username', 'admin').strip(),
                        password=row.get('Password', 'admin').strip(),
                        vendor_type=row.get('Vendor', 'arista').strip().lower(),
                        site=row.get('Site', '').strip(),
                        group_name=row.get('Group', '').strip(),
                        tags=row.get('Tags', '').strip(),
                        notes=row.get('Notes', '').strip(),
                        api_port=int(row['API Port']) if row.get('API Port', '').strip() else None,
                        api_key=row.get('API Key', '').strip(),
                    )
                    count += 1
            _log_action(request.user, 'device_add',
                       details=f"Imported {count} devices", request=request)
            messages.success(request, f'Imported {count} devices!')
            return redirect('dashboard')
    else:
        form = DeviceImportForm()
    return render(request, 'connect/import_devices.html', {
        'form': form,
    })


# ============================================================
# AUDIT LOG
# ============================================================

@login_required
def audit_log(request):
    ip_filter = request.GET.get('ip', '').strip()
    qs = AuditLog.objects.select_related('user', 'device').all()
    if ip_filter:
        qs = qs.filter(ip_address=ip_filter)
    logs = qs[:500]
    return render(request, 'connect/audit_log.html', {
        'logs': logs, 'devices': Device.objects.all(), 'ip_filter': ip_filter,
    })


@login_required
def access_by_ip(request):
    """List all client IPs that have accessed the server — full detail view.

    Shows per-IP summary (users, first/last seen, action count), the full
    AuditLog event trail (user + action + details + timestamp), and the raw
    page-visit log from RequestLog (path + method + status + timestamp).
    Optionally filters to a single IP via ?ip= query param.
    """
    import socket

    ip_filter = request.GET.get('ip', '').strip()

    # --- AuditLog-based data ---
    audit_qs = AuditLog.objects.filter(ip_address__isnull=False).exclude(ip_address='')
    if ip_filter:
        audit_qs = audit_qs.filter(ip_address=ip_filter)

    # Per-IP summary: first/last seen, total actions, distinct users
    summary_rows = (
        audit_qs.values('ip_address')
        .annotate(
            action_count=Count('id'),
            first_seen=Min('timestamp'),
            last_seen=Max('timestamp'),
        )
        .order_by('-last_seen')
    )

    # Enrich summary with usernames and resolve hostnames
    summary = []
    for row in summary_rows:
        ip = row['ip_address']
        users = list(
            AuditLog.objects.filter(ip_address=ip, user__isnull=False)
            .values_list('user__username', flat=True)
            .distinct()
        )
        try:
            hostname = socket.gethostbyaddr(ip)[0]
        except Exception:
            hostname = ''
        summary.append({
            'ip_address': ip,
            'hostname': hostname,
            'users': users,
            'action_count': row['action_count'],
            'first_seen': row['first_seen'],
            'last_seen': row['last_seen'],
        })

    # Full AuditLog event trail
    events = (
        audit_qs.select_related('user', 'device')
        .values(
            'timestamp', 'ip_address',
            'user__username', 'action', 'details',
        )
        .order_by('-timestamp')[:2000]
    )

    # --- RequestLog-based page visit data ---
    req_qs = RequestLog.objects.select_related('user')
    if ip_filter:
        req_qs = req_qs.filter(ip_address=ip_filter)
    page_visits = req_qs.values(
        'timestamp', 'ip_address', 'user__username', 'method', 'path', 'status_code',
    ).order_by('-timestamp')[:2000]

    # Summary for page visits per IP
    visit_summary = (
        RequestLog.objects.filter(ip_address__isnull=False)
        .values('ip_address')
        .annotate(visit_count=Count('id'), last_visit=Max('timestamp'))
        .order_by('-last_visit')
    ) if not ip_filter else []

    return render(request, 'connect/access_by_ip.html', {
        'summary': summary,
        'events': events,
        'page_visits': page_visits,
        'visit_summary': visit_summary,
        'ip_filter': ip_filter,
    })


# ============================================================
# TOPOLOGY (Enhanced with LLDP engine)
# ============================================================

@login_required
def topology_map(request):
    from .models import LabTopology
    recent = list(LabTopology.objects.order_by('-updated_at')[:3])
    return render(request, 'connect/topology.html', {
        'devices': Device.objects.all(),
        'lab_topo_recent': recent,
    })


def _topology_scan_background():
    """Run discover_topology in a worker thread (updates TopologyLink / ChassisDeviceLink in DB)."""
    try:
        discover_topology()
    except Exception as e:
        logger.exception('Background topology scan failed: %s', e)


def _append_external_topology_peers(nodes, links):
    """Merge unresolved LLDP peers for Checkmk-style External LLDP layer."""
    from .topology import collect_external_lldp_peers

    try:
        ext_nodes, ext_links = collect_external_lldp_peers()
    except Exception as exc:
        logger.warning('collect_external_lldp_peers: %s', exc)
        return nodes, links
    return list(nodes) + list(ext_nodes), list(links) + list(ext_links)


@login_required
def topology_data(request):
    """Return topology from DB cache. Use ?refresh=1 only for synchronous full scan (rare)."""
    from .topology_flags import TOPOLOGY_GRAPH_V3
    refresh = request.GET.get('refresh', '')
    if refresh == '1':
        data = discover_topology()
        return JsonResponse(data)
    if TOPOLOGY_GRAPH_V3:
        from .topology_graph import build_global_graph
        graph = build_global_graph(lldp=True)
        nodes = graph.get('nodes') or []
        links = graph.get('links') or []
        nodes, links = _append_external_topology_peers(nodes, links)
        return JsonResponse({
            'nodes': nodes,
            'links': links,
            'needs_rescan': len(nodes) > 0 and len(links) == 0,
            '_graph_builder': True,
        })
    data = get_cached_topology()
    return JsonResponse({
        'nodes': data.get('nodes', []),
        'links': data.get('links', []),
        'needs_rescan': len(data.get('links') or []) == 0,
    })


@login_required
def topology_refresh(request):
    """Start LLDP topology scan in the background (non-blocking)."""
    t = threading.Thread(target=_topology_scan_background, name='topology_scan', daemon=True)
    t.start()
    messages.info(
        request,
        'Topology scan started in the background. Refresh the page in a few seconds to see updated links.',
    )
    return redirect('topology')


@login_required
@require_POST
def topology_rescan(request):
    """POST: start background topology scan; returns JSON immediately."""
    # Invalidate graph cache so next graph.json poll gets fresh data
    try:
        from .topology_graph import invalidate_cache
        invalidate_cache(None)
    except Exception:
        pass
    t = threading.Thread(target=_topology_scan_background, name='topology_scan', daemon=True)
    t.start()
    return JsonResponse({
        'status': 'started',
        'message': 'Scan started in background. Refresh to see results.',
    })


@login_required
def topology_export(request):
    """
    Export tag-filtered network topology for draw.io or JSON consumers.
    Query: format=drawio|json, tags=comma-separated (same OR + component rule as UI).
    """
    from django.http import HttpResponse

    from .topology_export import (
        filter_topology_subgraph,
        parse_tags_param,
        topology_export_json,
        topology_to_drawio_xml,
    )
    from .topology_flags import TOPOLOGY_GRAPH_V3

    fmt = (request.GET.get('format') or 'drawio').strip().lower()
    tag_list = parse_tags_param(request.GET.get('tags', ''))

    if TOPOLOGY_GRAPH_V3:
        from .topology_graph import build_global_graph
        graph = build_global_graph(lldp=True)
        nodes = list(graph.get('nodes') or [])
        links = list(graph.get('links') or [])
        nodes, links = _append_external_topology_peers(nodes, links)
    else:
        data = get_cached_topology() or {}
        nodes = list(data.get('nodes') or [])
        links = list(data.get('links') or [])

    fnodes, flinks = filter_topology_subgraph(nodes, links, tag_list)
    slug = '-'.join(sorted(tag_list)[:4]) if tag_list else 'all'
    if len(tag_list) > 4:
        slug += '-etc'

    if fmt == 'json':
        payload = topology_export_json(
            fnodes,
            flinks,
            title='LabVault topology',
            tags=tag_list,
        )
        resp = HttpResponse(
            json.dumps(payload, indent=2),
            content_type='application/json',
        )
        resp['Content-Disposition'] = f'attachment; filename="labvault-topology-{slug}.json"'
        return resp

    xml_body = topology_to_drawio_xml(
        fnodes,
        flinks,
        diagram_name='LabVault topology' + (f' ({", ".join(tag_list)})' if tag_list else ''),
    )
    resp = HttpResponse(xml_body, content_type='application/xml')
    resp['Content-Disposition'] = f'attachment; filename="labvault-topology-{slug}.drawio"'
    return resp


@login_required
def device_enable_lldp(request, device_id):
    """Enable LLDP on a specific device."""
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)
    result = driver.enable_lldp()
    if result.success:
        data = result.data or {}
        count = data.get('enabled_count', 0)
        messages.success(request, f'LLDP enabled on {device.hostname or device.ip_address}: {count} interfaces configured.')
        _log_action(request.user, 'config_change',
                   details=f"LLDP enabled: {count} interfaces. {'; '.join(data.get('details', [])[:3])}",
                   device=device, request=request)
    else:
        messages.error(request, f'LLDP enable failed: {result.error}')
    return redirect('device_detail', device_id=device.id)


# ============================================================
# DEVICE COMPARISON
# ============================================================

@login_required
def device_compare(request):
    device1_id = request.GET.get('device1')
    device2_id = request.GET.get('device2')
    comparison = None
    if device1_id and device2_id:
        d1 = get_object_or_404(Device, id=device1_id)
        d2 = get_object_or_404(Device, id=device2_id)
        comparison = {
            'device1': d1, 'device2': d2,
            'fields': [
                ('Hostname', d1.hostname, d2.hostname),
                ('IP Address', d1.ip_address, d2.ip_address),
                ('Vendor', d1.vendor_display, d2.vendor_display),
                ('Model', d1.model_name, d2.model_name),
                ('Version', d1.version, d2.version),
                ('Serial', d1.serial_number, d2.serial_number),
                ('Status', d1.get_status_display(), d2.get_status_display()),
                ('Uptime', d1.uptime_display, d2.uptime_display),
                ('Site', d1.site, d2.site),
                ('Group', d1.group_name, d2.group_name),
            ],
        }
    return render(request, 'connect/device_compare.html', {
        'devices': Device.objects.all(), 'comparison': comparison,
        'device1_id': device1_id, 'device2_id': device2_id,
    })


# ============================================================
# FLEET REPORTS (NEW)
# ============================================================

@login_required
def fleet_report(request):
    """Executive fleet dashboard with heatmap, firmware tracker, top consumers."""
    devices = Device.objects.all()
    total = devices.count()
    online = devices.filter(status='online').count()
    offline = devices.filter(status='offline').count()
    vendor_stats = list(devices.values('vendor_type').annotate(count=Count('id')))
    site_stats = list(devices.exclude(site='').values('site').annotate(count=Count('id')))

    # Firmware tracker
    firmware_list = list(devices.filter(version__isnull=False).exclude(version='')
                        .values('vendor_type', 'version', 'model_name')
                        .annotate(count=Count('id')).order_by('vendor_type', 'version'))

    # Top CPU/Memory consumers (from recent snapshots)
    recent_snapshots = DeviceSnapshot.objects.filter(
        timestamp__gte=timezone.now() - timedelta(hours=1)
    ).values('device__hostname', 'device__ip_address', 'device__id').annotate(
        avg_cpu=Avg('cpu_utilization'),
        avg_mem=Avg('memory_used'),
    ).order_by('-avg_cpu')[:10]

    return render(request, 'connect/fleet_report.html', {
        'devices': devices, 'total': total, 'online': online, 'offline': offline,
        'vendor_stats': vendor_stats, 'site_stats': site_stats,
        'firmware_list': firmware_list,
        'top_consumers': list(recent_snapshots),
    })


@login_required
def inventory_report(request):
    """Exportable inventory report."""
    devices = Device.objects.all().order_by('vendor_type', 'hostname')
    fmt = request.GET.get('format', 'html')

    if fmt == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="inventory_report.csv"'
        writer = csv.writer(response)
        writer.writerow(['Hostname', 'IP Address', 'Vendor', 'Model', 'Version',
                         'Serial Number', 'Status', 'Site', 'Group', 'Uptime', 'Last Seen'])
        for d in devices:
            writer.writerow([d.hostname or '', d.ip_address, d.vendor_display,
                           d.model_name or '', d.version or '', d.serial_number or '',
                           d.status, d.site, d.group_name, d.uptime_display,
                           d.last_seen.strftime('%Y-%m-%d %H:%M') if d.last_seen else ''])
        return response

    return render(request, 'connect/inventory_report.html', {
        'devices': devices, 'all_devices': Device.objects.all(),
    })


@login_required
def sla_report(request):
    """Uptime SLA report."""
    hours = int(request.GET.get('hours', 720))  # Default 30 days
    since = timezone.now() - timedelta(hours=hours)

    devices = Device.objects.all()
    report = []
    for d in devices:
        total_snapshots = DeviceSnapshot.objects.filter(
            device=d, timestamp__gte=since).count()
        online_snapshots = DeviceSnapshot.objects.filter(
            device=d, timestamp__gte=since, uptime__gt=0).count()
        availability = round(online_snapshots / total_snapshots * 100, 2) if total_snapshots else 0
        report.append({
            'device': d,
            'total_checks': total_snapshots,
            'online_checks': online_snapshots,
            'availability': availability,
        })

    return render(request, 'connect/sla_report.html', {
        'devices': Device.objects.all(), 'report': report,
        'hours': hours, 'days': hours // 24,
    })


@login_required
def change_log_report(request):
    """Unified timeline: down/up, moves, deploy events, and config backups."""
    from .models import ChangeLogEvent, KeysightBmcEndpoint, KeysightChassis

    device_id = request.GET.get('device_id', '')
    chassis_id = request.GET.get('chassis_id', '')
    bmc_id = request.GET.get('bmc_id', '')
    target_kind = request.GET.get('target_kind', '')
    event_type = request.GET.get('event_type', '')
    days = int(request.GET.get('days', 30))
    since = timezone.now() - timedelta(days=days)

    events_qs = ChangeLogEvent.objects.filter(created_at__gte=since)
    if event_type:
        events_qs = events_qs.filter(event_type=event_type)
    if target_kind:
        events_qs = events_qs.filter(target_kind=target_kind)
    if device_id:
        events_qs = events_qs.filter(target_kind='device', target_id=device_id)
    if chassis_id:
        events_qs = events_qs.filter(target_kind='chassis', target_id=chassis_id)
    if bmc_id:
        events_qs = events_qs.filter(target_kind='bmc', target_id=bmc_id)

    events = list(events_qs.order_by('-created_at')[:500])

    config_rows = []
    backups = ConfigBackup.objects.filter(created_at__gte=since).select_related('device', 'created_by')
    if device_id:
        backups = backups.filter(device_id=device_id)
    if not chassis_id and not event_type:
        for b in backups.order_by('-created_at')[:50]:
            config_rows.append({
                'created_at': b.created_at,
                'event_type': 'config_change',
                'event_label': 'Config Change',
                'target_repr': str(b.device),
                'old_value': '',
                'new_value': 'diff' if b.diff_from_previous else 'snapshot',
                'detail': b.device.hostname or b.device.ip_address,
                'actor_username': getattr(b.created_by, 'username', '') or 'System',
                'ip_address': None,
                'user_agent': '',
                'backup_id': b.id,
                'has_diff': bool(b.diff_from_previous),
            })

    event_type_choices = ChangeLogEvent.EVENT_TYPE_CHOICES

    return render(request, 'connect/change_log_report.html', {
        'devices': Device.objects.all().order_by('ip_address'),
        'chassis_list': KeysightChassis.objects.all().order_by('ip_address'),
        'bmc_list': KeysightBmcEndpoint.objects.select_related('chassis').order_by('hostname'),
        'events': events,
        'config_rows': config_rows,
        'device_id': device_id,
        'chassis_id': chassis_id,
        'bmc_id': bmc_id,
        'target_kind': target_kind,
        'event_type': event_type,
        'days': days,
        'event_type_choices': event_type_choices,
        'target_kind_choices': ChangeLogEvent.TARGET_KIND_CHOICES,
        'total_events': events_qs.count(),
    })


# ============================================================
# SETTINGS PAGE (webhooks, scheduled jobs, API tokens, maintenance)
# ============================================================

@login_required
def settings_view(request):
    """Settings page for webhooks, scheduled jobs, API tokens, etc."""
    webhooks = WebhookEndpoint.objects.all()
    jobs = ScheduledJob.objects.all()
    tokens = APIToken.objects.filter(user=request.user)
    windows = MaintenanceWindow.objects.all().order_by('-start_time')[:20]
    groups = DeviceGroup.objects.all()

    return render(request, 'connect/settings.html', {
        'devices': Device.objects.all(),
        'webhooks': webhooks,
        'scheduled_jobs': jobs,
        'api_tokens': tokens,
        'maintenance_windows': windows,
        'device_groups': groups,
    })


@login_required
def about_view(request):
    """About LabVault — design credit and installed library versions."""
    from .about_info import about_context
    return render(request, 'connect/about.html', about_context())


@login_required
@require_POST
def create_webhook(request):
    name = request.POST.get('name', '')
    url = request.POST.get('url', '')
    wh_type = request.POST.get('webhook_type', 'generic')
    severity_filter = request.POST.get('severity_filter', '')
    if name and url:
        WebhookEndpoint.objects.create(
            name=name, url=url, webhook_type=wh_type,
            severity_filter=severity_filter)
        messages.success(request, f'Webhook "{name}" created.')
    return redirect('settings')


@login_required
@require_POST
def delete_webhook(request, webhook_id):
    wh = get_object_or_404(WebhookEndpoint, id=webhook_id)
    wh.delete()
    return JsonResponse({'status': 'ok'})


@login_required
@require_POST
def create_api_token(request):
    name = request.POST.get('name', 'API Token')
    token = secrets.token_hex(32)
    APIToken.objects.create(user=request.user, name=name, token=token)
    messages.success(request, f'API Token created: {token}')
    return redirect('settings')


@login_required
@require_POST
def revoke_api_token(request, token_id):
    token = get_object_or_404(APIToken, id=token_id, user=request.user)
    token.enabled = False
    token.save()
    return JsonResponse({'status': 'ok'})


@login_required
@require_POST
def create_maintenance_window(request):
    name = request.POST.get('name', '')
    start = request.POST.get('start_time', '')
    end = request.POST.get('end_time', '')
    device_ids = request.POST.getlist('device_ids')
    if name and start and end:
        try:
            window = MaintenanceWindow.objects.create(
                name=name,
                start_time=timezone.make_aware(datetime.fromisoformat(start)),
                end_time=timezone.make_aware(datetime.fromisoformat(end)),
                created_by=request.user,
            )
            for did in device_ids:
                try:
                    device = Device.objects.get(id=did)
                    MaintenanceWindowDevice.objects.create(window=window, device=device)
                except Device.DoesNotExist:
                    pass
            messages.success(request, f'Maintenance window "{name}" created.')
        except Exception as e:
            messages.error(request, f'Error: {e}')
    return redirect('settings')


@login_required
@require_POST
def create_scheduled_job(request):
    name = request.POST.get('name', '')
    job_type = request.POST.get('job_type', 'config_backup')
    schedule = request.POST.get('schedule', 'daily')
    if name:
        ScheduledJob.objects.create(
            name=name, job_type=job_type, schedule=schedule,
            created_by=request.user)
        messages.success(request, f'Job "{name}" created.')
    return redirect('settings')


@login_required
@require_POST
def create_device_group(request):
    name = request.POST.get('name', '')
    description = request.POST.get('description', '')
    parent_id = request.POST.get('parent_group', '')
    if name:
        parent = DeviceGroup.objects.filter(id=parent_id).first() if parent_id else None
        DeviceGroup.objects.create(name=name, description=description, parent_group=parent)
        messages.success(request, f'Group "{name}" created.')
    return redirect('settings')


# ============================================================
# LIVE STATUS (AJAX)
# ============================================================

@login_required
def device_live_status(request):
    devices = Device.objects.all().values(
        'id', 'ip_address', 'hostname', 'status', 'vendor_type',
        'model_name', 'version', 'uptime', 'last_seen', 'serial_number')
    data = []
    for d in devices:
        d['last_seen'] = d['last_seen'].isoformat() if d['last_seen'] else None
        data.append(d)
    return JsonResponse({'devices': data, 'timestamp': timezone.now().isoformat()})


@login_required
def device_quick_status(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    return JsonResponse({
        'status': device.status,
        'last_seen': device.last_seen.isoformat() if device.last_seen else None,
        'uptime': device.uptime_display,
    })


# ============================================================
# REST API
# ============================================================

@login_required
def api_devices(request):
    devices = Device.objects.all()
    data = [{'id': d.id, 'ip_address': d.ip_address, 'hostname': d.hostname,
             'vendor_type': d.vendor_type, 'version': d.version,
             'serial_number': d.serial_number, 'model_name': d.model_name,
             'status': d.status, 'uptime': d.uptime,
             'last_seen': d.last_seen.isoformat() if d.last_seen else None,
             'tags': d.tag_list, 'site': d.site, 'group': d.group_name,
             } for d in devices]
    return JsonResponse({'devices': data})


@login_required
def api_device_detail(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    cached = _get_cached_data(device_id)
    return JsonResponse({
        'id': device.id, 'ip_address': device.ip_address, 'hostname': device.hostname,
        'vendor_type': device.vendor_type, 'version': device.version,
        'serial_number': device.serial_number, 'model_name': device.model_name,
        'status': device.status, 'interfaces': cached or {},
    })


@login_required
def api_device_health(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    health = fetch_device_health(device)
    if health:
        return JsonResponse({'status': 'ok', 'data': health})
    return JsonResponse({'status': 'error', 'message': 'Cannot reach device'}, status=503)


@login_required
def api_device_ocs_patch(request, device_id):
    """Return patch-line pairs from the same in-memory cache as the device page."""
    device = get_object_or_404(Device, id=device_id)
    if device.vendor_type != 'ocs':
        return JsonResponse({'error': 'not_ocs_device'}, status=400)
    cached = _get_cached_data(device_id) or _get_stale_cached_data(device_id)
    ent = _get_cache_entry(device_id)
    cache_time = ent['timestamp'].isoformat() if ent else None
    if not cached:
        return JsonResponse({'pairs': [], 'count': 0, 'cache_time': cache_time})
    pairs = cached.get('ocs_patch_pairs') or []
    return JsonResponse({'pairs': pairs, 'count': len(pairs), 'cache_time': cache_time})


def api_execute_command(request, device_id):
    """Removed from the customer SKU — use allowlisted driver verbs only."""
    return JsonResponse({'error': 'not_available'}, status=404)


@login_required
def api_alerts(request):
    alerts = Alert.objects.all()
    if request.GET.get('unack'):
        alerts = alerts.filter(acknowledged=False)
    data = [{'id': a.id, 'device': str(a.device), 'type': a.alert_type,
             'severity': a.severity, 'message': a.message,
             'acknowledged': a.acknowledged, 'created_at': a.created_at.isoformat(),
             } for a in alerts[:100]]
    return JsonResponse({'alerts': data})


@login_required
def api_device_ocs_mapping(request, device_id):
    """
    Return the OCS site mapping for a device (from [ocs_site_mapping] in notes).
    Also returns current cross-connect state from the OCS device cache if available.
    Used by the Lab Topology designer OCS panel.
    """
    device = get_object_or_404(Device, id=device_id)
    notes = device.notes or ""

    import re as _re
    m = _re.search(r'\[ocs_site_mapping\]\s*(\{.+\})', notes)
    if not m:
        return JsonResponse({
            'device_id': device_id,
            'to_ocs': None,
            'port_to_ocs_triplets': {},
            'active_xconns': {},
            'ocs_device_id': None,
        })

    try:
        mapping = json.loads(m.group(1))
    except json.JSONDecodeError:
        mapping = {}

    to_ocs_ip = mapping.get('to_ocs', '')
    port_triplets = mapping.get('port_to_ocs_triplets', {})

    # Try to find the OCS device
    ocs_dev = None
    if to_ocs_ip:
        ocs_dev = Device.objects.filter(ip_address=to_ocs_ip, vendor_type='ocs').first()

    # Collect all referenced triplets
    all_triplets = set()
    for trips in port_triplets.values():
        if isinstance(trips, list):
            all_triplets.update(trips)
        elif isinstance(trips, str):
            all_triplets.add(trips)

    # Look up active cross-connects from OCS device cache
    active_xconns = {}
    if ocs_dev:
        cached = _get_cached_data(ocs_dev.id) or _get_stale_cached_data(ocs_dev.id)
        if cached:
            xconns = cached.get('ocs_crossconnects') or []
            for xc in xconns:
                # Index by h1 and h2 inp triplets
                for half in ('h1', 'h2'):
                    h = xc.get(half, {})
                    inp = str(h.get('inp', '') or '')
                    if inp in all_triplets:
                        active_xconns[inp] = {
                            'state': str(h.get('os', '') or h.get('state', '') or ''),
                            'power': str(h.get('op', '') or ''),
                            'loss': str(h.get('loss', '') or ''),
                        }

    return JsonResponse({
        'device_id': device_id,
        'to_ocs': to_ocs_ip,
        'to_ocs_name': ocs_dev.hostname if ocs_dev else to_ocs_ip,
        'port_to_ocs_triplets': port_triplets,
        'active_xconns': active_xconns,
        'ocs_device_id': ocs_dev.id if ocs_dev else None,
        'vendor_type_secondary': device.vendor_type_secondary or '',
    })


@login_required
def api_topology(request):
    """REST API endpoint for topology data."""
    data = get_cached_topology()
    return JsonResponse(data)



def _customer_sku_gone(feature: str = 'feature'):
    """Hard-dumped customer-SKU surface — always 404 JSON."""
    from django.http import JsonResponse
    return JsonResponse({'error': 'not_available', 'feature': feature}, status=404)


def api_hw_assignments_resolve(request):
    return _customer_sku_gone('hw_assignments_resolve')


def api_hw_assignments_release(request):
    return _customer_sku_gone('hw_assignments_release')


def ai_nexus(request):
    return _customer_sku_gone('ai_nexus')


def api_nexus_graph_data(request):
    return _customer_sku_gone('ai_nexus')


def api_nexus_graph_query(request):
    return _customer_sku_gone('ai_nexus')


def api_nexus_plugin_status(request):
    return _customer_sku_gone('ai_nexus')


def api_nexus_sessions(request):
    return _customer_sku_gone('ai_nexus')



# ─── OCS Patch Snapshot: Backup & Restore ─────────────────────────────────────

@csrf_exempt
@_api_auth_required
@require_http_methods(['GET', 'POST'])
def api_ocs_snapshot_create(request, device_id):
    """POST: save current OCS patch state as a named snapshot.

    Body (JSON): {"name": "...", "description": "..."}
    Fetches live cross-connect list from the OCS device, converts to restore-
    compatible pairs, and persists in OcsPatchSnapshot.
    """
    device = get_object_or_404(Device, id=device_id, vendor_type='ocs')
    if request.method == 'GET':
        snaps = device.ocs_patch_snapshots.all()[:50]
        data = [
            {
                'id': s.pk,
                'name': s.name or f'Snapshot {s.pk}',
                'description': s.description,
                'patch_count': s.patch_count,
                'created_at': s.created_at.isoformat(),
                'created_by': s.created_by.username if s.created_by else None,
            }
            for s in snaps
        ]
        return JsonResponse({'snapshots': data, 'device_id': device_id})

    # POST – capture live state
    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    snap_name = (body.get('name') or '').strip() or f'Snapshot {datetime.now():%Y-%m-%d %H:%M}'
    snap_desc = (body.get('description') or '').strip()

    driver = get_driver(device)
    raw_rows = []
    try:
        raw_rows = driver.fetch_crossconnect_list()
    except Exception as e:
        return JsonResponse({'error': f'Failed to fetch OCS state: {e}'}, status=502)

    # Build minimal restore-friendly pairs from raw rows
    import re as _re
    restore_pairs = []
    for row in raw_rows or []:
        if not isinstance(row, dict):
            continue
        h1 = row.get('half1') or {}
        conn_str = str(h1.get('conn') or '')
        m = _re.match(r'([^>]+)>([^>]+)', conn_str)
        if not m:
            continue
        from connect.drivers.ocs import _norm_dir
        restore_pairs.append({
            'in': m.group(1).strip(),
            'out': m.group(2).strip(),
            'conn': str(row.get('name') or row.get('connid') or ''),
            'group': str(row.get('group') or 'SYSTEM'),
            'dir': _norm_dir(row.get('dir') or 'bi'),
            'band': str(row.get('band') or ''),
        })

    snap = OcsPatchSnapshot.objects.create(
        device=device,
        name=snap_name,
        description=snap_desc,
        raw_xconns=raw_rows,
        patch_pairs=restore_pairs,
        patch_count=len(restore_pairs),
        created_by=request.user,
    )
    _log_action(request.user, 'ocs_snapshot_create', device=device,
                details=f'Snapshot "{snap_name}" ({len(restore_pairs)} patches)', request=request)
    return JsonResponse({
        'ok': True,
        'snapshot_id': snap.pk,
        'name': snap.name,
        'patch_count': snap.patch_count,
    })


def _normalize_ocs_restore_connections(pairs: list) -> list:
    """Normalize snapshot pairs for OCS ``xconnect_badd`` (dir casing, drop empties)."""
    from connect.drivers.ocs import _norm_dir

    out = []
    for p in pairs or []:
        if not isinstance(p, dict):
            continue
        row = {k: v for k, v in p.items() if v not in (None, '')}
        if 'dir' in row:
            row['dir'] = _norm_dir(row['dir'])
        if row.get('in') and row.get('out'):
            out.append(row)
    return out


@csrf_exempt
@_api_auth_required
@require_http_methods(['GET', 'DELETE'])
def api_ocs_snapshot_detail(request, snapshot_id):
    """GET: snapshot details.  DELETE: remove snapshot."""
    snap = get_object_or_404(OcsPatchSnapshot, pk=snapshot_id)
    if request.method == 'DELETE':
        device = snap.device
        snap_name = snap.name
        snap.delete()
        _log_action(request.user, 'ocs_snapshot_delete', device=device,
                    details=f'Deleted snapshot "{snap_name}"', request=request)
        return JsonResponse({'ok': True})
    return JsonResponse({
        'id': snap.pk,
        'name': snap.name,
        'description': snap.description,
        'patch_count': snap.patch_count,
        'patch_pairs': snap.patch_pairs,
        'created_at': snap.created_at.isoformat(),
        'created_by': snap.created_by.username if snap.created_by else None,
    })


@csrf_exempt
@_api_auth_required
@require_http_methods(['GET'])
def api_ocs_snapshot_download(request, snapshot_id):
    """Download snapshot as JSON (raw cross-connects + restore pairs)."""
    snap = get_object_or_404(OcsPatchSnapshot, pk=snapshot_id)
    payload = {
        'id': snap.pk,
        'device_id': snap.device_id,
        'device_ip': snap.device.ip_address,
        'name': snap.name,
        'description': snap.description,
        'patch_count': snap.patch_count,
        'patch_pairs': snap.patch_pairs,
        'raw_xconns': snap.raw_xconns,
        'created_at': snap.created_at.isoformat(),
        'created_by': snap.created_by.username if snap.created_by else None,
    }
    safe_name = re.sub(r'[^\w.\-]+', '_', (snap.name or f'snapshot_{snap.pk}'))[:80]
    response = HttpResponse(
        json.dumps(payload, indent=2),
        content_type='application/json',
    )
    response['Content-Disposition'] = (
        f'attachment; filename="ocs-patch-{safe_name}-{snap.pk}.json"'
    )
    return response


@csrf_exempt
@_api_auth_required
@require_http_methods(['POST'])
def api_ocs_snapshot_restore(request, snapshot_id):
    """POST: restore an OCS snapshot.

    Body (JSON): {"clear_first": true|false}
    Optionally clears all current cross-connects before restoring.
    Uses xconnect_badd for efficiency (bulk add).
    """
    snap = get_object_or_404(OcsPatchSnapshot, pk=snapshot_id)
    device = snap.device

    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    clear_first = bool(body.get('clear_first', True))

    driver = get_driver(device)
    connections = _normalize_ocs_restore_connections(snap.patch_pairs or [])

    results = []
    errors = []
    method = 'rest'
    if not (snap.patch_pairs or []):
        errors.append('Snapshot has no patch pairs to restore')
    else:
        try:
            res = driver.restore_patch_snapshot(connections, clear_first=clear_first)
            method = (res.data or {}).get('method', 'rest') if res.data else 'rest'
            results.append({'ok': res.success, 'detail': res.data, 'method': method})
            if not res.success:
                errors.append(res.error or 'unknown error')
        except Exception as e:
            errors.append(str(e))
            results.append({'ok': False, 'detail': str(e)})

    # Bust cache so UI refreshes with new patch state
    _clear_cached_data(device.id)

    _log_action(request.user, 'ocs_snapshot_restore', device=device,
                details=f'Restored snapshot "{snap.name}" ({snap.patch_count} patches), '
                        f'clear_first={clear_first}, method={method}', request=request)
    if errors:
        return JsonResponse({'ok': False, 'errors': errors, 'results': results}, status=207)
    return JsonResponse({
        'ok': True,
        'results': results,
        'patch_count': snap.patch_count,
        'method': method,
    })



def api_research_index(request):
    return _customer_sku_gone('agent_research')


def api_research_graph(request):
    return _customer_sku_gone('agent_research')


def api_research_output(request, output_id):
    return _customer_sku_gone('agent_research')


def api_research_rebuild(request):
    return _customer_sku_gone('agent_research')

