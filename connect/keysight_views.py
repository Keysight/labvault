# HARD_DUMP_REMOVED — customer stub/compat
"""
Keysight / Ixia chassis management views for LabVault.
All views are prefixed with keysight_ and live under /keysight/ URL namespace.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import sys
from pathlib import Path
from functools import lru_cache, wraps
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import close_old_connections
from django.db import models as db_models
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .ip_addressing import enrich_lldp_neighbors_for_display
from .keysight_drivers import get_driver, KCOS_TYPES
from .keysight_discovery import scan_subnet_async, get_scan_status, auto_add_discovered
from .cache_utils import cache_delete, cache_get, cache_set
from .models import (
    AuditLog, KeysightChassis, KeysightChassisSnapshot, KeysightSubnetScan,
    KeysightReservation, KeysightReservationItem, KeysightDeploymentJob,
    KeysightBmcEndpoint, KEYSIGHT_CHASSIS_TYPE_CHOICES,
)

logger = logging.getLogger(__name__)

# ============================================================
# CHASSIS DATA CACHE (shared across gunicorn workers via Django cache)
# ============================================================
_KS_CACHE_KEY_PREFIX = 'keysight:chassis_data:'
_KS_CACHE_TTL = 600  # seconds — keep ahead of live heartbeat interval (120s)


def _set_cached(chassis_id: int, data: dict):
    payload = dict(data)
    payload['_cached_at'] = time.time()
    cache_set(f'{_KS_CACHE_KEY_PREFIX}{chassis_id}', payload, _KS_CACHE_TTL)


def _get_cached(chassis_id: int) -> dict | None:
    entry = cache_get(f'{_KS_CACHE_KEY_PREFIX}{chassis_id}')
    if entry and (time.time() - entry.get('_cached_at', 0)) < _KS_CACHE_TTL:
        return entry
    return None


def _clear_cached(chassis_id: int):
    cache_delete(f'{_KS_CACHE_KEY_PREFIX}{chassis_id}')


def _purge_chassis_timeseries(chassis_id: int) -> None:
    """Remove np_timeseries rows before chassis delete (cross-DB FK is DO_NOTHING)."""
    from .models import NPResourceSample, PortUsageSample

    try:
        NPResourceSample.objects.filter(chassis_id=chassis_id).delete()
    except Exception:
        logger.debug('NPResourceSample purge failed for chassis %s', chassis_id, exc_info=True)
    try:
        PortUsageSample.objects.filter(chassis_id=chassis_id).delete()
    except Exception:
        logger.debug('PortUsageSample purge failed for chassis %s', chassis_id, exc_info=True)


def _delete_keysight_chassis(ch: KeysightChassis) -> None:
    """Delete chassis and related rows without cross-DB CASCADE errors."""
    cid = ch.id
    _purge_chassis_timeseries(cid)
    ch.delete()
    _clear_cached(cid)


def _ensure_chassis_data_cached(
    chassis_list,
    *,
    max_workers: int = 6,
    max_fetch: int | None = 32,
) -> tuple[int, int]:
    """Fetch chassis API data for online units missing from cache.

    Returns (fetched_count, total_missing). ``max_fetch`` caps synchronous work
    on cold cache so HW inventory does not block for the full fleet.
    """
    missing = [ch for ch in chassis_list if ch.status == 'online' and not _get_cached(ch.id)]
    total_missing = len(missing)
    if not missing:
        return 0, 0
    if max_fetch is not None and len(missing) > max_fetch:
        missing = missing[:max_fetch]

    def _fetch_one(ch):
        try:
            if probe_chassis(ch) == 'ok':
                fetch_chassis_data(ch)
        except Exception:
            logger.debug('On-demand KS fetch failed for %s', ch.ip_address, exc_info=True)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(missing))) as pool:
        pool.map(_fetch_one, missing)
    return len(missing), total_missing


# ============================================================
# HELPERS
# ============================================================

def _is_kcos(chassis) -> bool:
    return chassis.chassis_type in KCOS_TYPES


def _ixos_rg_block(number: int, port_list: list, mode_label: str = '') -> dict:
    return {
        'number': number,
        'label': f'RG{number:02d}',
        'title': f'Resource Group {number:02d} (RG{number:02d})',
        'mode_label': mode_label,
        'ports': port_list,
    }


def _ixos_heuristic_resource_groups_for_ports(ports: list) -> list | None:
    """XGS12-style layouts when topology/REST does not name RGs (matches native CMC)."""
    plist = sorted(ports or [], key=lambda x: x.get('port_number', 0))
    n = len(plist)
    if n == 2:
        return [_ixos_rg_block(1, plist[:1]), _ixos_rg_block(2, plist[1:])]
    if n == 4:
        return [_ixos_rg_block(1, plist[:2]), _ixos_rg_block(2, plist[2:])]
    if n == 8:
        return [_ixos_rg_block(1, plist[:4]), _ixos_rg_block(2, plist[4:])]
    return None


def _ixos_resource_groups_from_rest_fields(ports: list) -> list | None:
    """Build RGs when every port includes resource_group_number from IxOS REST."""
    if not ports:
        return None
    from collections import defaultdict
    if not all('resource_group_number' in p for p in ports):
        return None
    if any(p.get('resource_group_number') is None for p in ports):
        return None
    by_rg: dict[int, list] = defaultdict(list)
    for p in ports:
        by_rg[int(p['resource_group_number'])].append(p)
    out = []
    for rgn in sorted(by_rg.keys()):
        out.append(_ixos_rg_block(rgn, sorted(by_rg[rgn], key=lambda x: x.get('port_number', 0))))
    return out or None


def _ixos_cmc_flat_card_type(card: dict) -> bool:
    """Card types where native Chassis Management Console omits RG headers (flat port row only)."""
    ctype = (card.get('type') or '').lower()
    return '40ge2ng' in ctype or '10ge8ng' in ctype


def _ixos_flatten_resource_groups_like_cmc(card: dict) -> None:
    """Drop RG stripes when they duplicate per-lane topology but CMC shows a single port strip."""
    rgs = card.get('resource_groups') or []
    if not rgs:
        return
    if _ixos_cmc_flat_card_type(card):
        card['resource_groups'] = []
        return
    sizes = [len(g.get('ports') or []) for g in rgs]
    # e.g. 8× "Resource Group" with one 10GE each — CMC does not label those as RG rows
    if len(rgs) >= 6 and sizes and all(s <= 1 for s in sizes):
        card['resource_groups'] = []


def _ixos_maybe_split_monolithic_resource_group(card: dict) -> None:
    """If SSH collapsed the whole card into one unnamed RG, split 2/4/8-port cards like CMC."""
    if _ixos_cmc_flat_card_type(card):
        return
    rgs = card.get('resource_groups') or []
    if len(rgs) != 1:
        return
    ports = sorted(card.get('ports') or [], key=lambda x: x.get('port_number', 0))
    rg_ports = sorted(rgs[0].get('ports') or [], key=lambda x: x.get('port_number', 0))
    if len(ports) < 2 or len(rg_ports) != len(ports):
        return
    if {p.get('port_number') for p in ports} != {p.get('port_number') for p in rg_ports}:
        return
    if (rgs[0].get('mode_label') or '').strip():
        return
    inferred = _ixos_heuristic_resource_groups_for_ports(ports)
    if inferred:
        card['resource_groups'] = inferred


def _client_meta(request):
    """Client IP and User-Agent for AuditLog (user_agent column is NOT NULL in DB)."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        ip = xff.split(',')[0].strip() or None
    else:
        ip = request.META.get('REMOTE_ADDR') or None
    ua = (request.META.get('HTTP_USER_AGENT') or '')[:500]
    return ip, ua


def _log_action(request, action, chassis=None, details=''):
    ip, ua = _client_meta(request)
    AuditLog.objects.create(
        user=request.user if request.user.is_authenticated else None,
        action='other',
        details=f'[Keysight] {action}: {details}',
        ip_address=ip,
        user_agent=ua,
    )


def _audit_log(request, action, details):
    """Keysight reservation / misc audit rows using standard action codes."""
    ip, ua = _client_meta(request)
    AuditLog.objects.create(
        user=request.user,
        action=action,
        details=details,
        ip_address=ip,
        user_agent=ua,
    )


def _chassis_target_repr(ch: KeysightChassis) -> str:
    return ch.hostname or ch.ip_address or f'chassis:{ch.pk}'


def _installed_version_for_package(ch: KeysightChassis, package_type: str) -> str:
    if package_type == 'kcos':
        return (ch.kcos_version or '').strip()
    return (ch.kcos_version or '').strip()


def _apply_deployment_job_actor(job: KeysightDeploymentJob, request) -> None:
    ip, ua = _client_meta(request)
    job.started_ip = ip
    job.started_user_agent = ua
    job.save(update_fields=['started_ip', 'started_user_agent', 'updated_at'])


def _actor_from_deployment_job(job: KeysightDeploymentJob) -> dict:
    from .request_audit import parse_user_agent

    username = ''
    if job.started_by_id:
        username = job.started_by.get_username() or ''
    ua = job.started_user_agent or ''
    parsed = parse_user_agent(ua)
    return {
        'username': username,
        'ip_address': job.started_ip,
        'user_agent': ua,
        **parsed,
    }


def _deploy_operation_extra(job: KeysightDeploymentJob, ch: KeysightChassis) -> dict:
    return {
        'package_type': job.package_type,
        'job_type': job.job_type,
        'job_id': job.id,
        'batch_id': job.batch_id,
        'chassis_type': ch.chassis_type,
    }


def _log_deploy_start_changelog(request, job: KeysightDeploymentJob) -> None:
    from .changelog import log_deploy_lifecycle
    from .request_audit import classify_version_change, request_actor_context

    ch = job.chassis
    old_v = _installed_version_for_package(ch, job.package_type)
    actor = request_actor_context(request)
    target = _chassis_target_repr(ch)
    pkg_label = job.get_package_type_display()
    detail = (
        f'{pkg_label} {job.job_type} started (job {job.id}, batch {job.batch_id}) '
        f'on {ch.get_chassis_type_display()}'
    )
    extra = _deploy_operation_extra(job, ch)
    log_deploy_lifecycle(
        'deploy_start',
        chassis_id=ch.id,
        target_repr=target,
        detail=detail,
        old_value=old_v,
        new_value=job.target_version,
        actor=actor,
        extra_json=extra,
    )
    direction = classify_version_change(old_v, job.target_version)
    if direction in ('upgrade', 'downgrade'):
        log_deploy_lifecycle(
            direction,
            chassis_id=ch.id,
            target_repr=target,
            detail=f'{pkg_label}: {old_v or "unknown"} → {job.target_version}',
            old_value=old_v,
            new_value=job.target_version,
            actor=actor,
            extra_json=extra,
        )


def _log_deploy_terminal_changelog(job: KeysightDeploymentJob, status: str, message: str | None) -> None:
    from .changelog import log_deploy_lifecycle

    if status not in ('success', 'error'):
        return
    event_type = 'deploy_done' if status == 'success' else 'deploy_failed'
    ch = job.chassis
    actor = _actor_from_deployment_job(job)
    pkg_label = job.get_package_type_display()
    detail = (message or job.message or '')[:500]
    if not detail:
        detail = f'{pkg_label} {job.job_type} {status}'
    log_deploy_lifecycle(
        event_type,
        chassis_id=ch.id,
        target_repr=_chassis_target_repr(ch),
        detail=detail,
        old_value=_installed_version_for_package(ch, job.package_type),
        new_value=job.target_version,
        actor=actor,
        extra_json=_deploy_operation_extra(job, ch),
    )


def _log_chassis_operation(
    request,
    *,
    event_type: str,
    ch: KeysightChassis,
    detail: str,
    old_value: str = '',
    new_value: str = '',
    extra_json: dict | None = None,
) -> None:
    from .changelog import log_deploy_lifecycle
    from .request_audit import request_actor_context

    merged = dict(extra_json or {})
    merged['chassis_type'] = ch.chassis_type
    log_deploy_lifecycle(
        event_type,
        chassis_id=ch.id,
        target_repr=_chassis_target_repr(ch),
        detail=detail,
        old_value=old_value,
        new_value=new_value,
        actor=request_actor_context(request),
        extra_json=merged,
    )


# ============================================================
# DATA FETCHING
# ============================================================

def fetch_chassis_data(chassis) -> dict | None:
    driver = get_driver(chassis)
    is_kcos = _is_kcos(chassis)

    # --- ALL API calls in parallel (including chassis_info) ---
    from concurrent.futures import ThreadPoolExecutor

    futures = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures['info'] = pool.submit(driver.get_chassis_info)
        futures['cards'] = pool.submit(driver.get_cards)
        futures['ports'] = pool.submit(driver.get_ports)
        futures['health'] = pool.submit(driver.get_health)
        futures['sensors'] = pool.submit(driver.get_sensors)
        if is_kcos:
            futures['logical_ports'] = pool.submit(driver.get_logical_ports)
            futures['front_panel'] = pool.submit(driver.get_front_panel_ports)
            futures['deployed_apps'] = pool.submit(driver.get_deployed_apps)
            def _fetch_bps():
                from .keysight_drivers.bps import BPSDriver
                drv = BPSDriver(chassis.connect_address, chassis.username, chassis.password)
                return drv.get_topology()
            futures['bps_topo'] = pool.submit(_fetch_bps)
        else:
            futures['ssh_topo'] = pool.submit(driver.get_topology_ssh)
            futures['ssh_lldp'] = pool.submit(driver.get_lldp_ssh)

    def _safe(key):
        f = futures.get(key)
        if not f:
            return None
        try:
            return f.result(timeout=30)
        except Exception as exc:
            logger.warning('fetch_chassis_data %s failed for %s: %s', key, chassis.ip_address, exc)
            return None

    # --- Process chassis info ---
    info_result = _safe('info')
    if info_result and info_result.success and info_result.data:
        info = info_result.data
        chassis.serial_number = info.get('serial_number', '')
        chassis.controller_serial = info.get('controller_serial', '')
        chassis.chassis_state = info.get('state', 'unknown')
        chassis.num_physical_cards = info.get('num_physical_cards', 0)
        chassis.status = 'online'
        chassis.last_seen = timezone.now()

        if is_kcos:
            chassis.os_platform = 'kcos'
            chassis.kcos_version = info.get('kcos_version', '')
            chassis.hostname = info.get('hostname', '') or chassis.hostname
            chassis.os_type = 'Linux'
            chart_name = info.get('kcos_chart_name', '').lower()
            hostname = (chassis.hostname or info.get('hostname', '') or '').lower()
            # TREX/HTREX detection (KCOS-based, connections API often empty)
            if 'htrex' in hostname or 'h-trex' in hostname or 'htrex' in chart_name:
                chassis.chassis_type = 'aresone_htrex'
            elif 'trex' in hostname or 't-rex' in hostname or 'trex' in chart_name:
                chassis.chassis_type = 'trex'
            elif 'kcos-400' in chart_name or 'eagle-merlin' in chart_name:
                chassis.chassis_type = 'aps_m8400'
            elif chart_name:
                chassis.chassis_type = 'aps_m1010'
            else:
                if 'm8400' in hostname:
                    chassis.chassis_type = 'aps_m8400'
                elif 'm1010' in hostname:
                    chassis.chassis_type = 'aps_m1010'
        else:
            chassis.os_platform = 'ixos'
            chassis.ixos_version = info.get('ixos_version', '')
            chassis.os_type = 'Linux'
            chassis.ixos_applications = json.dumps(info.get('ixos_applications', {}))
            api_type = info.get('chassis_type', '')
            normalized = api_type.lower().replace('-', '').replace('_', '').replace(' ', '')
            if 'aresone' in normalized:
                chassis.chassis_type = 'aresone'
            elif 'xgs2' in normalized:
                chassis.chassis_type = 'xgs2'
            elif 'xgs12' in normalized:
                chassis.chassis_type = 'xgs12'
            elif 'xm' in normalized:
                chassis.chassis_type = 'xm'
            elif 'xg' in normalized and 'xgs' not in normalized:
                chassis.chassis_type = 'xg'
        chassis.save()
    else:
        return None

    # IxOS ARESONE fallback: detect from card types (800GE, 400GBASE) when chassis_type missed
    if not is_kcos and chassis.chassis_type != 'aresone':
        cards_result = _safe('cards')
        cards_data = cards_result.data if cards_result and cards_result.success else []
        for card in (cards_data if isinstance(cards_data, list) else []):
            card_type = (card.get('type') or '').lower()
            if '800ge' in card_type or '400gbase' in card_type:
                chassis.chassis_type = 'aresone'
                chassis.save(update_fields=['chassis_type'])
                logger.info('ARESONE detected from card type %r for %s', card_type, chassis.ip_address)
                break

    cards_result = _safe('cards')
    cards = cards_result.data if cards_result and cards_result.success else []

    ports_result = _safe('ports')
    ports = ports_result.data if ports_result and ports_result.success else []

    is_aresone = chassis.chassis_type == 'aresone'

    # --- SSH topology: build port_display_map & resource_groups from CLI ---
    ssh_topo_data = None
    port_display_map: dict[tuple[int, int], str] = {}
    ssh_rg_by_card: dict[int, list] = {}

    if not is_kcos:
        ssh_topo_result = _safe('ssh_topo')
        if ssh_topo_result and ssh_topo_result.success and ssh_topo_result.data:
            ssh_topo_data = ssh_topo_result.data
            port_display_map = driver.correlate_ports(ssh_topo_data, ports)
            for card_num, card_info in ssh_topo_data.get('cards', {}).items():
                ssh_rg_by_card[int(card_num)] = card_info.get('resource_groups', [])

    # Assign port_display: SSH map first, then AresONE fallback, then raw number
    for p in ports:
        cn = p.get('card_number', 0)
        pn = p.get('port_number')
        key = (cn, pn)
        if key in port_display_map:
            p['port_display'] = port_display_map[key]
        elif is_aresone and pn is not None and 9 <= pn <= 24:
            rg = (pn - 9) // 2 + 1
            sub = (pn - 9) % 2 + 1
            p['port_display'] = f'{rg}.{sub}'
        else:
            p['port_display'] = str(pn) if pn is not None else ''

    ports_by_card = {}
    for p in ports:
        cn = p['card_number']
        ports_by_card.setdefault(cn, []).append(p)

    from collections import defaultdict

    for card in cards:
        card_num = card['card_number']
        card['ports'] = ports_by_card.get(card_num, [])
        card['ports_up'] = sum(1 for p in card['ports'] if p.get('link_state') == 'up')
        card['ports_total'] = len(card['ports'])
        card['ports_owned'] = sum(1 for p in card['ports'] if p.get('owner', 'Free') != 'Free')
        card['resource_groups'] = []

        # Prefer SSH-parsed resource groups (works for all IxOS: AresONE, XGS2, XGS12 etc.)
        if card_num in ssh_rg_by_card and ssh_rg_by_card[card_num]:
            for rg_info in ssh_rg_by_card[card_num]:
                rg_display_names = [pe['display'] for pe in rg_info.get('ports', [])]
                rg_ports = [p for p in card['ports'] if p.get('port_display') in rg_display_names]
                card['resource_groups'].append({
                    'number': rg_info['number'],
                    'label': rg_info['label'],
                    'title': f"Resource Group {rg_info['number']:02d} ({rg_info['label']})",
                    'mode_label': rg_info.get('mode', ''),
                    'ports': sorted(rg_ports, key=lambda x: x.get('port_number', 0)),
                })
        elif is_aresone and card.get('ports'):
            # Fallback: hardcoded AresONE 9-24 mapping when SSH unavailable
            rg_map = defaultdict(list)
            for p in card['ports']:
                pn = p.get('port_number')
                if pn is not None and 9 <= pn <= 24:
                    rg = (pn - 9) // 2 + 1
                    rg_map[rg].append(p)
            for rg_num in sorted(rg_map.keys()):
                card['resource_groups'].append({
                    'number': rg_num,
                    'label': f'RG{rg_num:02d}',
                    'title': f'Resource Group {rg_num:02d} (RG{rg_num:02d})',
                    'mode_label': '2x400GBASE-CR4',
                    'ports': sorted(rg_map[rg_num], key=lambda x: x.get('port_number', 0)),
                })
        elif not is_kcos and card.get('ports'):
            plist = sorted(card['ports'], key=lambda x: x.get('port_number', 0))
            rest_rg = _ixos_resource_groups_from_rest_fields(plist)
            if rest_rg:
                card['resource_groups'] = rest_rg
            elif _ixos_cmc_flat_card_type(card):
                card['resource_groups'] = []
            else:
                inferred = _ixos_heuristic_resource_groups_for_ports(plist)
                if inferred:
                    card['resource_groups'] = inferred
                else:
                    card['resource_groups'].append({
                        'number': 1,
                        'label': 'PORTS',
                        'title': 'Ports',
                        'mode_label': '',
                        'ports': plist,
                    })

    if not is_kcos:
        for card in cards:
            _ixos_maybe_split_monolithic_resource_group(card)
        for card in cards:
            _ixos_flatten_resource_groups_like_cmc(card)

    # AresONE hostname generation for topology matching (LLDP reports "ares1-{serial}")
    if is_aresone and chassis.serial_number and not chassis.hostname:
        chassis.hostname = f"ares1-{chassis.serial_number.lower()}"
        chassis.save(update_fields=['hostname'])

    # --- Chassis LLDP (24h persistent cache; live scan overrides per port) ---
    # IxOS: SSH (fastest, pre-fetched) → REST → persisted cache → DB reverse-lookup
    # KCOS: SNMP → persisted cache → DB reverse-lookup
    # Other platforms: persisted cache → DB reverse-lookup
    from .lldp_persistence import get_entity_neighbors, persist_entity_neighbors
    from datetime import timedelta
    from django.utils import timezone as _tz
    from django.db.models import Q

    lldp_neighbors = []
    live_lldp_fetch = False

    if not is_kcos:
        # 1) SSH LLDP via "show lldp-peer-info data" (pre-fetched in thread pool — ~3s)
        ssh_lldp_result = _safe('ssh_lldp')
        if ssh_lldp_result and ssh_lldp_result.success and ssh_lldp_result.data:
            lldp_neighbors = ssh_lldp_result.data
            live_lldp_fetch = True

        # 2) IxOS REST embedded lldpPeerData
        if not lldp_neighbors:
            try:
                lp = driver.get_lldp_peers()
                if lp.success and lp.data:
                    lldp_neighbors = list(lp.data)
                    live_lldp_fetch = True
            except Exception as e:
                logger.debug('IxOS REST LLDP for %s: %s', chassis.ip_address, e)
    elif is_kcos:
        # KCOS: try SNMP (usually responsive on KCOS, unlike IxOS)
        try:
            from .topology_lldp import fetch_chassis_lldp
            comm = (getattr(chassis, 'snmp_community', None) or '').strip() or 'public'
            lldp_neighbors = fetch_chassis_lldp(chassis.ip_address, community=comm, timeout=3)
            live_lldp_fetch = bool(lldp_neighbors)
        except Exception as e:
            logger.debug('Chassis LLDP SNMP for %s: %s', chassis.ip_address, e)

    if live_lldp_fetch:
        lldp_neighbors = persist_entity_neighbors(
            'chassis', chassis.id, lldp_neighbors, fresh_scan_ok=True,
        )
    else:
        lldp_neighbors = get_entity_neighbors('chassis', chassis.id)

    # 3) Reverse-populate from ChassisDeviceLink DB (built by topology engine from
    #    device-side LLDP — e.g. Sonic reports seeing the chassis as its neighbor)
    if not lldp_neighbors:
        try:
            from .models import ChassisDeviceLink
            from .lldp_persistence import LLDP_RETENTION_SECONDS
            cutoff = _tz.now() - timedelta(seconds=LLDP_RETENTION_SECONDS)
            db_links = ChassisDeviceLink.objects.filter(chassis=chassis).filter(
                Q(link_status='up') | Q(last_seen__gte=cutoff),
            ).select_related('device')
            for cl in db_links:
                lldp_neighbors.append({
                    'local_port': cl.port_chassis,
                    'remote_device': cl.device.hostname or cl.device.ip_address,
                    'remote_port': cl.port_device,
                    'chassis_id': '',
                    'mgmt_ip': cl.device.ip_address,
                })
        except Exception as e:
            logger.debug('Reverse LLDP from DB for %s: %s', chassis.ip_address, e)

    try:
        from .topology_lldp import merge_lldp_into_cards
        merge_lldp_into_cards(cards, lldp_neighbors)
    except Exception as e:
        logger.debug('merge_lldp_into_cards for %s: %s', chassis.ip_address, e)

    health_result = _safe('health')
    health = health_result.data if health_result and health_result.success else {}

    sensors_result = _safe('sensors')
    sensors = sensors_result.data if sensors_result and sensors_result.success else []

    logical_ports = []
    fpga_ports = []
    deployed_apps = []
    if is_kcos:
        lp_result = _safe('logical_ports')
        if lp_result and lp_result.success:
            lp_data = lp_result.data
            if isinstance(lp_data, list):
                logical_ports = lp_data
            elif isinstance(lp_data, dict):
                logical_ports = (lp_data.get('logicalports') or lp_data.get('ports')
                                 or lp_data.get('data') or
                                 (list(lp_data.values())[0] if lp_data else []))
                if not isinstance(logical_ports, list):
                    logical_ports = []
        fp_result = _safe('front_panel')
        if fp_result and fp_result.success:
            fp_data = fp_result.data
            if isinstance(fp_data, list):
                fpga_ports = fp_data
            elif isinstance(fp_data, dict):
                fpga_ports = (fp_data.get('frontpanel') or fp_data.get('ports')
                              or fp_data.get('data') or
                              (list(fp_data.values())[0] if fp_data else []))
                if not isinstance(fpga_ports, list):
                    fpga_ports = []
        da_result = _safe('deployed_apps')
        if da_result and da_result.success and isinstance(da_result.data, list):
            deployed_apps = da_result.data

    # --- BPS topology ---
    bps_topology = {'slots': []}
    bps_l23_engines = []
    bps_model = ''
    if is_kcos:
        topo_result = _safe('bps_topo')
        if topo_result and topo_result.success and isinstance(topo_result.data, dict):
            bps_topology = topo_result.data
            bps_l23_engines = [s for s in bps_topology.get('slots', []) if s.get('is_l23')]
            logger.info('BPS topology for %s: %d slots, %d L23',
                        chassis.ip_address, len(bps_topology.get('slots', [])), len(bps_l23_engines))

            # BPS model-based type correction (authoritative for multi-node chassis)
            bps_raw = bps_topology.get('raw', {})
            bps_model = ''
            if isinstance(bps_raw, dict):
                bps_model = (bps_raw.get('model', '') or '').lower()
            if bps_model and chassis.chassis_type != 'aps_standalone':
                old_type = chassis.chassis_type
                if 'm8400' in bps_model:
                    chassis.chassis_type = 'aps_m8400'
                elif 'm1010' in bps_model or 'm1020' in bps_model:
                    chassis.chassis_type = 'aps_m1010'
                if chassis.chassis_type != old_type:
                    chassis.save(update_fields=['chassis_type'])
                    logger.info('BPS model "%s" corrected type %s -> %s for %s',
                                bps_model, old_type, chassis.chassis_type, chassis.ip_address)
        elif topo_result:
            logger.info('BPS topology no data for %s: %s', chassis.ip_address, topo_result.error)

    # Standalone APS: single KCOS node (mgmt role=compute) — not a multi-CN M1010/M8400
    if chassis.os_platform == 'kcos' and chassis.chassis_type not in ('aresone_htrex', 'trex'):
        from .keysight_aps_standalone import infer_chassis_type_from_kcos, is_standalone_kcos_api_nodes

        try:
            nodes_result = driver._get('/introspection/nodes')
            api_nodes = (
                nodes_result.data
                if nodes_result.success and isinstance(nodes_result.data, list)
                else []
            )
            standalone = is_standalone_kcos_api_nodes(api_nodes)
            chart_name = ''
            if info_result and info_result.success and info_result.data:
                chart_name = info_result.data.get('kcos_chart_name', '') or ''
            new_type = infer_chassis_type_from_kcos(
                is_standalone=standalone,
                chart_name=chart_name,
                hostname=chassis.hostname,
                bps_model=bps_model if is_kcos else '',
                current_type=chassis.chassis_type,
            )
            if new_type != chassis.chassis_type:
                old_type = chassis.chassis_type
                chassis.chassis_type = new_type
                chassis.save(update_fields=['chassis_type'])
                logger.info(
                    'KCOS topology %s -> %s for %s (standalone=%s)',
                    old_type, new_type, chassis.ip_address, standalone,
                )
        except Exception as exc:
            logger.debug('Standalone APS detection failed for %s: %s', chassis.ip_address, exc)

    # --- Fallback: if KCOS fpga_ports is empty, synthesize from BPS L23 engines ---
    if is_kcos and not fpga_ports and bps_l23_engines:
        fpga_ports = [
            {
                'name': f"Slot {e['id']}",
                'engine': e['id'],
                'model': e.get('model', ''),
                'pcsLinkStatus': 'UP',
                'mode': e.get('mode', ''),
                'fanout': e.get('fanout', ''),
                'status': 'active',
            }
            for e in bps_l23_engines
        ]
        logger.info('Using %d BPS L23 engines as FPGA fallback for %s', len(fpga_ports), chassis.ip_address)

    # --- owners_summary: group ports by owner (exclude Free); include M8400 front-panel reservedBy
    owners_map = {}  # owner -> {'count': n, 'locations': [str, ...]}
    for p in ports:
        owner = (p.get('owner') or 'Free').strip()
        if not owner or owner == 'Free':
            continue
        cn = p.get('card_number', '')
        pn = p.get('port_number', '')
        loc = f"{cn}/{pn}" if cn != '' and pn != '' else (str(cn) or str(pn) or '')
        if owner not in owners_map:
            owners_map[owner] = {'count': 0, 'locations': []}
        owners_map[owner]['count'] += 1
        if loc:
            owners_map[owner]['locations'].append(loc)
    if is_kcos and chassis.chassis_type == 'aps_m8400':
        for slot in bps_topology.get('slots', []):
            slot_id = slot.get('id', '')
            for pp in slot.get('physical_ports', []):
                for lane in pp.get('lanes', []):
                    reserved = (lane.get('reservedBy') or '').strip()
                    if not reserved or reserved == 'Free':
                        continue
                    loc = f"Slot {slot_id}/{lane.get('id', '')}"
                    if reserved not in owners_map:
                        owners_map[reserved] = {'count': 0, 'locations': []}
                    owners_map[reserved]['count'] += 1
                    owners_map[reserved]['locations'].append(loc)
    owners_summary = [
        {'owner': k, 'count': v['count'], 'locations': v['locations']}
        for k, v in sorted(owners_map.items())
    ]

    data = {
        'cards': cards,
        'ports': ports,
        'health': health,
        'sensors': sensors,
        'total_ports': len(ports),
        'ports_up': sum(1 for p in ports if p.get('link_state') == 'up'),
        'ports_free': sum(1 for p in ports if p.get('owner', 'Free') == 'Free'),
        'ports_owned': sum(1 for p in ports if p.get('owner', 'Free') != 'Free'),
        'is_kcos': is_kcos,
        'is_aresone': is_aresone,
        'logical_ports': logical_ports,
        'fpga_ports': fpga_ports,
        'deployed_apps': deployed_apps,
        'bps_topology': bps_topology,
        'bps_l23_engines': bps_l23_engines,
        'owners_summary': owners_summary,
        'lldp_neighbors': enrich_lldp_neighbors_for_display(lldp_neighbors),
        'lldp_count': len(lldp_neighbors),
    }

    try:
        KeysightChassisSnapshot.objects.create(
            chassis=chassis,
            cpu_utilization=health.get('cpu_utilization', 0),
            memory_used=health.get('memory_used', 0),
            memory_total=health.get('memory_total', 0),
        )
    except Exception:
        pass

    _set_cached(chassis.id, data)
    return data


def probe_chassis(chassis) -> str:
    old_status = chassis.status
    driver = get_driver(chassis)
    status = driver.probe()
    if status == 'ok':
        chassis.status = 'online'
        chassis.last_seen = timezone.now()
    elif status == 'auth_failed':
        chassis.status = 'auth_failed'
    else:
        chassis.status = 'offline'
    chassis.save()
    try:
        from .changelog import log_status_transition
        log_status_transition(
            'chassis',
            chassis.id,
            str(chassis.hostname or chassis.ip_address or chassis.pk),
            old_status,
            chassis.status,
        )
    except Exception:
        logger.exception('changelog status for chassis %s', chassis.pk)
    return status


# ============================================================
# BACKGROUND REFRESH
# ============================================================
_ks_refresh_started = False
_ks_refresh_lock = threading.Lock()
_ks_refresh_lock_fd = None
KS_REFRESH_INTERVAL = 120  # background refresh every 2 min (was 30s)
_KS_REFRESH_LEADER_LOCK = os.environ.get(
    'LABVAULT_KS_REFRESH_LOCK',
    str(Path(__file__).resolve().parent.parent / 'var' / 'keysight-refresh.leader'),
)


def _try_acquire_ks_refresh_leader():
    """Only one gunicorn worker should probe all chassis (avoids 4× network load)."""
    try:
        lock_path = _KS_REFRESH_LEADER_LOCK
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        return None


def _ks_refresh_all():
    while True:
        try:
            close_old_connections()
            chassis_list = list(KeysightChassis.objects.all())
            if chassis_list:

                def _refresh_one(ch):
                    close_old_connections()
                    try:
                        result = probe_chassis(ch)
                        if result == 'ok':
                            fetch_chassis_data(ch)
                    except Exception as e:
                        logger.debug(f'KS refresh error for {ch.ip_address}: {e}')
                    finally:
                        close_old_connections()

                with ThreadPoolExecutor(max_workers=min(8, len(chassis_list))) as pool:
                    pool.map(_refresh_one, chassis_list)
        except Exception as e:
            close_old_connections()
            logger.debug(f'KS background refresh error: {e}')
            from connect.worker_status import record_keysight_refresh
            record_keysight_refresh(error=str(e))
        finally:
            close_old_connections()
            from connect.worker_status import record_keysight_refresh
            record_keysight_refresh(leader=True)
        time.sleep(KS_REFRESH_INTERVAL)


def start_ks_refresh_thread():
    global _ks_refresh_started, _ks_refresh_lock_fd
    flag = (os.environ.get('LABVAULT_DISABLE_INPROCESS_REFRESH') or '').strip().lower()
    if flag in ('1', 'true', 'yes', 'on') or 'gunicorn' in sys.modules:
        return
    with _ks_refresh_lock:
        if _ks_refresh_started:
            return
        _ks_refresh_started = True
        _ks_refresh_lock_fd = _try_acquire_ks_refresh_leader()
        if _ks_refresh_lock_fd is None:
            logger.debug('Keysight background refresh skipped (another worker is leader)')
            from connect.worker_status import record_keysight_refresh
            record_keysight_refresh(leader=False)
            return
        t = threading.Thread(target=_ks_refresh_all, daemon=True)
        t.start()
        logger.info('Keysight background refresh thread started (leader worker)')


# ============================================================
# DASHBOARD
# ============================================================

@login_required
def keysight_refresh_node_slots(request):
    """Refresh KCOS mgmt/compute node associations, then return to the dashboard."""
    from django.shortcuts import redirect
    from django.urls import reverse

    from .keysight_dashboard_filters import keysight_dashboard_query_without
    from .keysight_node_associations import refresh_node_associations

    start_ks_refresh_thread()
    kcos_chassis = [ch for ch in KeysightChassis.objects.all() if _is_kcos(ch)]
    try:
        refresh_node_associations(kcos_chassis, _fetch_bmc_data_from_chassis)
    except Exception:
        logger.exception('keysight_refresh_node_slots failed')

    return redirect(
        reverse('keysight_dashboard') + keysight_dashboard_query_without(request, 'fetch_nodes'),
    )


@login_required
def keysight_dashboard(request):
    from django.shortcuts import redirect
    from django.urls import reverse

    from .keysight_aps_generations import (
        aps_generation_chip_stats,
        build_models_in_selection_breakdown,
    )
    from .keysight_dashboard_filters import (
        build_keysight_filter_chip_context,
        keysight_dashboard_query_without,
        keysight_filtered_chassis_list,
    )
    from .keysight_node_associations import (
        aggregate_slot_stats,
        enrich_associations_with_standalone_links,
        get_cached_node_associations,
    )
    from .keysight_hw_errors import (
        chassis_shows_hw_warning,
        count_hw_error_units,
        enrich_assoc_hw_flags,
        enrich_type_breakdown_hw_bad,
        build_selection_hw_summary,
    )

    if request.GET.get('fetch_nodes', '') == '1':
        return redirect(
            reverse('keysight_refresh_node_slots')
            + keysight_dashboard_query_without(request, 'fetch_nodes'),
        )

    start_ks_refresh_thread()
    all_chassis = list(KeysightChassis.objects.all())
    kcos_chassis = [ch for ch in all_chassis if _is_kcos(ch)]
    node_by_id = get_cached_node_associations([ch.id for ch in kcos_chassis])
    enrich_associations_with_standalone_links(node_by_id, all_chassis)
    enrich_assoc_hw_flags(node_by_id, [ch.id for ch in kcos_chassis])

    chip_ctx = build_keysight_filter_chip_context(
        request, all_chassis, all_chassis_list=all_chassis,
    )
    chassis_after_tag = keysight_filtered_chassis_list(
        request, node_by_id=node_by_id, chassis_pool=all_chassis,
    )

    chassis_ids = [ch.id for ch in chassis_after_tag if _is_kcos(ch)]
    node_by_id = {cid: node_by_id[cid] for cid in chassis_ids if cid in node_by_id}
    slot_stats = aggregate_slot_stats(node_by_id)
    aps_gen_stats = aps_generation_chip_stats(all_chassis, node_by_id)
    type_breakdown = build_models_in_selection_breakdown(chassis_after_tag, node_by_id)
    type_breakdown = enrich_type_breakdown_hw_bad(type_breakdown, chassis_after_tag, node_by_id)
    hw_summary = build_selection_hw_summary(chassis_after_tag, node_by_id)
    has_lab_chassis = bool(chassis_after_tag)

    reservation_map = get_reservations_summaries_for_chassis(
        [ch.id for ch in chassis_after_tag],
    )

    enriched = []
    for ch in chassis_after_tag:
        cached = _get_cached(ch.id)
        has_res, res_list = reservation_map.get(ch.id, (False, []))
        fqdn = (ch.hostname or ch.ip_address or '').strip()
        assoc = node_by_id.get(ch.id) if _is_kcos(ch) else None
        enriched.append({
            'chassis': ch,
            'data': cached or {},
            'reservations': res_list,
            'has_reservations': has_res,
            'fqdn': fqdn,
            'node_assoc': assoc,
            'hardware_error_reported': chassis_shows_hw_warning(ch, assoc),
            'hw_error_nodes': [
                n.get('name', '')
                for n in (assoc.get('compute_nodes') or []) if n.get('hardware_error_reported')
            ] if assoc else [],
        })
    # Sort: chassis with current/future reservations first
    enriched.sort(key=lambda e: (not e['has_reservations'], (e['chassis'].hostname or e['chassis'].ip_address)))
    reserved_chassis_count = sum(1 for e in enriched if e['has_reservations'])
    hw_error_count = count_hw_error_units(chassis_after_tag, node_by_id)
    total_online  = sum(1 for e in enriched if e['chassis'].status == 'online')
    total_offline = sum(1 for e in enriched if e['chassis'].status in ('offline', 'auth_failed'))

    bq = chip_ctx['build_qparams']
    aps_gen_stats_urls = []
    for ag in aps_gen_stats:
        aps_gen_stats_urls.append({
            **ag,
            'toggle_url': bq(aps_gen=('' if chip_ctx['aps_gen_filter'] == ag['value'] else ag['value'])),
            'is_selected': chip_ctx['aps_gen_filter'] == ag['value'],
        })

    context = {
        'chassis_list': enriched,
        'reserved_chassis_count': reserved_chassis_count,
        'hw_error_count': hw_error_count,
        'hw_summary': hw_summary,
        'total_chassis': len(enriched),
        'total_online': total_online,
        'total_offline': total_offline,
        **chip_ctx,
        'aps_gen_stats': aps_gen_stats_urls,
        'type_breakdown': type_breakdown,
        'has_lab_chassis': has_lab_chassis,
        **slot_stats,
        'node_assoc_cache_ready': bool(node_by_id),
        'node_refresh_url': reverse('keysight_refresh_node_slots')
        + keysight_dashboard_query_without(request, 'fetch_nodes'),
    }
    try:
        from connect.models import SensorAlarm
        from django.db.models import Count, Q
        hv_stats = SensorAlarm.objects.filter(
            acknowledged=False, resolved=False,
        ).aggregate(
            total=Count('id'),
            critical=Count('id', filter=Q(severity='critical')),
        )
        context['hv_alarm_count'] = hv_stats['total'] or 0
        context['hv_alarm_critical'] = hv_stats['critical'] or 0
    except Exception:
        context['hv_alarm_count'] = 0
        context['hv_alarm_critical'] = 0
    return render(request, 'connect/keysight/dashboard.html', context)


# ============================================================
# CHASSIS CRUD
# ============================================================

@login_required
def keysight_add_chassis(request):
    from .forms import KeysightChassisForm
    if request.method == 'POST':
        form = KeysightChassisForm(request.POST)
        if form.is_valid():
            ch = form.save()
            _log_action(request, 'add_chassis', chassis=ch, details=f'Added chassis {ch.ip_address}')
            threading.Thread(target=lambda: (probe_chassis(ch), fetch_chassis_data(ch)), daemon=True).start()
            messages.success(request, f'Chassis {ch.ip_address} added. Probing...')
            return redirect('keysight_dashboard')
    else:
        form = KeysightChassisForm()
    return render(request, 'connect/keysight/add_chassis.html', {'form': form})


@login_required
def keysight_edit_chassis(request, chassis_id):
    from .forms import KeysightEditChassisForm
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    if request.method == 'POST':
        form = KeysightEditChassisForm(request.POST, instance=ch)
        if form.is_valid():
            hw_changed = 'hardware_error_reported' in form.changed_data
            form.save()
            if hw_changed:
                from .keysight_slack import notify_hw_error_change
                notify_hw_error_change(
                    chassis=ch,
                    reported=ch.hardware_error_reported,
                    notes=ch.notes or '',
                    actor=request.user.get_full_name() or request.user.username,
                )
            _log_action(request, 'edit_chassis', chassis=ch, details=f'Edited chassis {ch.ip_address}')
            messages.success(request, f'Chassis {ch.ip_address} updated.')
            return redirect('keysight_chassis_detail', chassis_id=ch.id)
    else:
        form = KeysightEditChassisForm(instance=ch)
    return render(request, 'connect/keysight/edit_chassis.html', {'form': form, 'chassis': ch})


@login_required
@require_POST
def keysight_delete_chassis(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    ip = ch.ip_address
    try:
        _delete_keysight_chassis(ch)
    except Exception as exc:
        logger.exception('delete_chassis failed for %s', ip)
        messages.error(request, f'Could not delete chassis {ip}: {exc}')
        return redirect('keysight_chassis_detail', chassis_id=chassis_id)
    _log_action(request, 'delete_chassis', details=f'Deleted chassis {ip}')
    messages.success(request, f'Chassis {ip} deleted.')
    return redirect('keysight_dashboard')


@login_required
@require_POST
def keysight_set_node_hardware_error(request, chassis_id):
    """AJAX: flag/unflag a CN or mgmt node with a reported hardware issue."""
    from .keysight_hw_errors import resolve_bmc_endpoint

    ch = get_object_or_404(KeysightChassis, pk=chassis_id)
    node_name = (request.POST.get('node_name') or '').strip()
    bmc_hostname = (request.POST.get('bmc_hostname') or '').strip()
    reported = request.POST.get('reported', '0') in ('1', 'true', 'on', 'yes')
    notes = (request.POST.get('notes') or '').strip()
    if not node_name and not bmc_hostname:
        return JsonResponse({'ok': False, 'error': 'node_name or bmc_hostname required'}, status=400)
    ep = resolve_bmc_endpoint(ch, node_name=node_name, bmc_hostname=bmc_hostname)
    if not ep:
        return JsonResponse({'ok': False, 'error': 'Could not resolve BMC endpoint'}, status=400)
    ep.hardware_error_reported = reported
    ep.hardware_error_notes = notes if reported else ''
    ep.chassis = ch
    if node_name and not ep.node_name:
        ep.node_name = node_name
    ep.save(update_fields=[
        'hardware_error_reported', 'hardware_error_notes', 'chassis', 'node_name',
    ])
    _log_action(
        request, 'edit_chassis', chassis=ch,
        details=f'Node {node_name or bmc_hostname} hardware_error={reported}',
    )
    from .keysight_slack import notify_hw_error_change
    notify_hw_error_change(
        chassis=ch,
        node_name=ep.node_name or node_name,
        reported=reported,
        notes=notes if reported else '',
        actor=request.user.get_full_name() or request.user.username,
    )
    return JsonResponse({
        'ok': True,
        'node_name': ep.node_name,
        'bmc_hostname': ep.hostname,
        'hardware_error_reported': ep.hardware_error_reported,
        'hardware_error_notes': ep.hardware_error_notes,
    })


@login_required
@require_POST
def keysight_update_team_tags(request, chassis_id):
    """AJAX: update team_tags for a single chassis without a full page reload."""
    ch = get_object_or_404(KeysightChassis, pk=chassis_id)
    tags_raw = request.POST.get('team_tags', '')
    ch.team_tags = ','.join(t.strip() for t in tags_raw.split(',') if t.strip())
    ch.save(update_fields=['team_tags'])
    _log_action(request, 'edit_chassis', chassis=ch,
                details=f'Updated team_tags to "{ch.team_tags}"')
    return JsonResponse({'ok': True, 'team_tags': ch.team_tags,
                         'team_tags_list': ch.team_tags_list})


# ============================================================
# CHASSIS DETAIL
# ============================================================

@login_required
def keysight_chassis_detail(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    cached = _get_cached(ch.id)
    if not cached and ch.status in ('online', 'unknown'):
        cached = fetch_chassis_data(ch)
    # Refresh ch from DB in case fetch_chassis_data corrected the type
    ch.refresh_from_db()
    is_kcos = _is_kcos(ch)
    reservations = get_active_reservations_for_chassis(ch.id)
    is_m8400 = ch.chassis_type == 'aps_m8400'
    cached_data = cached or {}
    back_ports = cached_data.get('ports', [])
    bps_topo = cached_data.get('bps_topology', {})

    # Build unified port list: back-panel (KCOS connections) + front-panel (BPS lanes)
    all_ports = list(back_ports)  # already have panel_type='back'/'front' from driver
    if is_m8400:
        for slot in bps_topo.get('slots', []):
            for pp in slot.get('physical_ports', []):
                for lane in pp.get('lanes', []):
                    link_raw = lane.get('link', 'down')
                    link_up = link_raw.lower() in ('up',)
                    all_ports.append({
                        'id': f"fp-{slot['id']}-{lane['id']}",
                        'card_number': f"Slot {slot['id']}",
                        'port_number': lane['id'],
                        'owner': lane.get('reservedBy', '') or 'Free',
                        'link_state': 'up' if link_up else 'down',
                        'speed': lane.get('speed_display', ''),
                        'from_interface': pp.get('currentMode', ''),
                        'to_switch_port': f"Port {pp['id']}",
                        'fec_active': [],
                        'transceiver_mfg': '',
                        'transceiver_model': pp.get('transceiver_display', '') or '-',
                        'auto_negotiation': lane.get('media', ''),
                        'panel_type': 'front',
                    })

    # Hyperview SNMP integration hard-dumped on customer SKU.
    snmp_device = None
    snmp_sensors = []

    is_aresone = cached_data.get('is_aresone', ch.chassis_type == 'aresone')

    from .keysight_hw_errors import apply_hw_flags_to_cards
    cards = apply_hw_flags_to_cards(cached_data.get('cards', []), ch.id)

    context = {
        'chassis': ch,
        'data': cached_data,
        'cards': cards,
        'ports': all_ports,
        'health': cached_data.get('health', {}),
        'sensors': cached_data.get('sensors', []),
        'apps': json.loads(ch.ixos_applications) if ch.ixos_applications else {},
        'is_kcos': is_kcos,
        'is_m8400': is_m8400,
        'is_aresone': is_aresone,
        'logical_ports': cached_data.get('logical_ports', []),
        'fpga_ports': cached_data.get('fpga_ports', []),
        'deployed_apps': cached_data.get('deployed_apps', []),
        'bps_topology': bps_topo,
        'bps_l23_engines': cached_data.get('bps_l23_engines', []),
        'reservations': reservations,
        'snmp_device': snmp_device,
        'snmp_sensors': snmp_sensors,
        'lldp_neighbors': cached_data.get('lldp_neighbors', []),
        'lldp_count': cached_data.get('lldp_count', 0),
    }
    return render(request, 'connect/keysight/chassis_detail.html', context)


# ============================================================
# AJAX DATA
# ============================================================

@login_required
def keysight_chassis_data_json(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    cached = _get_cached(ch.id)
    if not cached:
        cached = fetch_chassis_data(ch) or {}
    return JsonResponse({
        'status': ch.status,
        'cards': cached.get('cards', []),
        'ports': cached.get('ports', []),
        'health': cached.get('health', {}),
        'total_ports': cached.get('total_ports', 0),
        'ports_up': cached.get('ports_up', 0),
        'ports_free': cached.get('ports_free', 0),
        'ports_owned': cached.get('ports_owned', 0),
        'owners_summary': cached.get('owners_summary', []),
    })


@login_required
def keysight_np_timeseries_json(request, chassis_id):
    get_object_or_404(KeysightChassis, id=chassis_id)
    range_label = request.GET.get('range') or '24h'
    return JsonResponse({'range': range_label, 'series': []})


@login_required
def keysight_dashboard_data_json(request):
    from .keysight_dashboard_filters import keysight_filtered_chassis_list
    chassis_list = keysight_filtered_chassis_list(request)
    result = []
    for ch in chassis_list:
        cached = _get_cached(ch.id) or {}
        result.append({
            'id': ch.id, 'ip_address': ch.ip_address, 'hostname': ch.hostname,
            'chassis_type': ch.get_chassis_type_display(), 'status': ch.status,
            'total_ports': cached.get('total_ports', 0),
            'ports_up': cached.get('ports_up', 0),
            'owners_summary': cached.get('owners_summary', []),
        })
    return JsonResponse({'chassis_list': result})


# ============================================================
# REST API FOR AGENTS / BPS WORKER (Bearer token auth)
# ============================================================

def _api_token_authenticate(request):
    """Authenticate via Authorization: Bearer <token>. Returns user or None."""
    auth = request.META.get('HTTP_AUTHORIZATION') or ''
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    try:
        from .models import APIToken
        api_token = APIToken.objects.get(token=token, enabled=True)
        if not api_token.is_valid:
            return None
        api_token.last_used = timezone.now()
        api_token.save(update_fields=['last_used'])
        return api_token.user
    except APIToken.DoesNotExist:
        return None


def _api_auth_required(view_func):
    """Decorator: allow session login OR Bearer token. Sets request.user if token valid."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view_func(request, *args, **kwargs)
        user = _api_token_authenticate(request)
        if user:
            request.user = user
            return view_func(request, *args, **kwargs)
        return JsonResponse({'error': 'Authentication required'}, status=401)
    return wrapper


@_api_auth_required
def api_keysight_resources(request):
    """
    REST API for agents/BPS worker: returns Keysight chassis list with connection info.
    Auth: session cookie OR Authorization: Bearer <api_token>
    Use this to discover BPS/IxOS chassis for running tests.
    """
    chassis_list = KeysightChassis.objects.all()
    result = []
    for ch in chassis_list:
        cached = _get_cached(ch.id) or {}
        result.append({
            'id': ch.id,
            'host': ch.ip_address,
            'hostname': ch.hostname or ch.ip_address,
            'username': ch.username,
            'password': ch.password,
            'chassis_type': ch.chassis_type,
            'status': ch.status,
            'total_ports': cached.get('total_ports', 0),
            'ports_up': cached.get('ports_up', 0),
            'ports_free': cached.get('ports_free', 0),
            'cards': cached.get('cards', []),
            'ports': cached.get('ports', []),
        })
    return JsonResponse({
        'chassis_list': result,
        'note': 'Use host, username, password with bps_login(host, user, password) to connect.',
    })


# ============================================================
# PORT / CARD OPERATIONS
# ============================================================

@login_required
@require_POST
def keysight_port_operation(request, chassis_id):
    from connect.demo_mode import mutation_blocked_response
    blocked = mutation_blocked_response('keysight_port_operation')
    if blocked:
        return blocked
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    driver = get_driver(ch)
    op = request.POST.get('operation', '')
    port_ids = request.POST.getlist('port_ids')

    if not port_ids:
        return JsonResponse({'success': False, 'error': 'No ports selected'})

    results = []
    for pid_str in port_ids:
        try:
            pid = int(pid_str)
        except ValueError:
            results.append({'port_id': pid_str, 'success': False, 'error': 'Invalid port ID'})
            continue
        if op == 'take_ownership':
            r = driver.take_ownership(pid)
        elif op == 'release_ownership':
            r = driver.release_ownership(pid)
        elif op == 'reboot':
            r = driver.reboot_port(pid)
        elif op == 'reset':
            r = driver.reset_port(pid)
        else:
            results.append({'port_id': pid, 'success': False, 'error': f'Unknown: {op}'})
            continue
        results.append({'port_id': pid, 'success': r.success, 'error': r.error})

    _clear_cached(chassis_id)
    return JsonResponse({'success': all(r['success'] for r in results), 'results': results})


@login_required
@require_POST
def keysight_card_operation(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    driver = get_driver(ch)
    op = request.POST.get('operation', '')
    card_id = request.POST.get('card_id', '')
    try:
        card_id = int(card_id)
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid card ID'})
    if op == 'hotswap':
        r = driver.hotswap_card(card_id)
    else:
        return JsonResponse({'success': False, 'error': f'Unknown: {op}'})
    _clear_cached(chassis_id)
    return JsonResponse({'success': r.success, 'error': r.error})


# ============================================================
# SENSORS & LICENSES
# ============================================================

@login_required
def keysight_chassis_sensors(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    driver = get_driver(ch)
    result = driver.get_sensors()
    sensors = result.data if result.success else []
    return render(request, 'connect/keysight/chassis_sensors.html', {'chassis': ch, 'sensors': sensors})


@login_required
def keysight_chassis_licenses(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    driver = get_driver(ch)
    result = driver.get_licenses()
    licenses = result.data if result.success else []
    return render(request, 'connect/keysight/chassis_licenses.html', {
        'chassis': ch, 'licenses': licenses, 'error': result.error if not result.success else '',
    })


# ============================================================
# KCOS OPERATIONS
# ============================================================

@login_required
@require_POST
def keysight_kcos_switch_app(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    if not _is_kcos(ch):
        return JsonResponse({'success': False, 'error': 'Not a KCOS chassis'})
    driver = get_driver(ch)
    node_name = request.POST.get('node_name', '')
    app_id = request.POST.get('app_id', '')
    force = request.POST.get('force', 'false').lower() == 'true'
    if not node_name or not app_id:
        return JsonResponse({'success': False, 'error': 'node_name and app_id required'})
    result = driver.switch_app(node_name, app_id, force=force)
    _clear_cached(chassis_id)
    return JsonResponse({'success': result.success, 'error': result.error, 'data': result.data if result.success else None})


@login_required
@require_POST
def keysight_kcos_power_cycle_node(request, chassis_id):
    from connect.demo_mode import mutation_blocked_response
    blocked = mutation_blocked_response('kcos_power_cycle_node')
    if blocked:
        return blocked
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    if not _is_kcos(ch):
        return JsonResponse({'success': False, 'error': 'Not a KCOS chassis'})
    driver = get_driver(ch)
    node_name = request.POST.get('node_name', '')
    if not node_name:
        return JsonResponse({'success': False, 'error': 'node_name required'})
    result = driver.power_cycle_node(node_name)
    _clear_cached(chassis_id)
    return JsonResponse({'success': result.success, 'error': result.error})


@login_required
@require_POST
def keysight_kcos_restart_node(request, chassis_id):
    from connect.demo_mode import mutation_blocked_response
    blocked = mutation_blocked_response('kcos_restart_node')
    if blocked:
        return blocked
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    if not _is_kcos(ch):
        return JsonResponse({'success': False, 'error': 'Not a KCOS chassis'})
    driver = get_driver(ch)
    node_name = request.POST.get('node_name', '')
    if not node_name:
        return JsonResponse({'success': False, 'error': 'node_name required'})
    result = driver.restart_node(node_name)
    _clear_cached(chassis_id)
    return JsonResponse({'success': result.success, 'error': result.error})


@login_required
@require_POST
def keysight_kcos_power_node(request, chassis_id):
    from connect.demo_mode import mutation_blocked_response
    blocked = mutation_blocked_response('kcos_power_node')
    if blocked:
        return blocked
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    if not _is_kcos(ch):
        return JsonResponse({'success': False, 'error': 'Not a KCOS chassis'})
    driver = get_driver(ch)
    node_name = request.POST.get('node_name', '')
    action = request.POST.get('action', '')
    if not node_name or action not in ('on', 'off'):
        return JsonResponse({'success': False, 'error': 'node_name and action (on/off) required'})
    result = driver.power_on_node(node_name) if action == 'on' else driver.power_off_node(node_name)
    _clear_cached(chassis_id)
    return JsonResponse({'success': result.success, 'error': result.error})


@login_required
@require_POST
def keysight_kcos_reboot_chassis(request, chassis_id):
    """Reboot the entire KCOS chassis via REST API POST /vital/control."""
    from connect.demo_mode import mutation_blocked_response
    blocked = mutation_blocked_response('kcos_reboot_chassis')
    if blocked:
        return blocked
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    if not _is_kcos(ch):
        return JsonResponse({'success': False, 'error': 'Not a KCOS chassis'})
    driver = get_driver(ch)
    result = driver.reboot_chassis()
    _clear_cached(chassis_id)
    return JsonResponse({'success': result.success, 'error': result.error})


@login_required
@require_POST
def keysight_clear_cache(request, chassis_id):
    """Clear the in-memory cache for this chassis and trigger a fresh data fetch."""
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    _clear_cached(chassis_id)
    # Trigger fresh fetch in background thread
    threading.Thread(target=lambda: fetch_chassis_data(ch), daemon=True).start()
    _log_action(request, 'clear_cache', chassis=ch, details=f'Cache cleared for {ch.ip_address}')
    return JsonResponse({'success': True, 'message': 'Cache cleared. Data is being refreshed.'})


@login_required
@require_POST
def keysight_bulk_redetect(request):
    """Re-detect chassis types for all KCOS chassis by clearing cache and
    re-fetching data (which includes chart-name-based auto-detection)."""
    kcos_chassis = KeysightChassis.objects.filter(chassis_type__in=list(KCOS_TYPES))
    count = kcos_chassis.count()

    def _bulk_refresh():
        for ch in kcos_chassis:
            _clear_cached(ch.id)
            try:
                fetch_chassis_data(ch)
            except Exception as exc:
                logger.warning('Bulk re-detect failed for %s: %s', ch.ip_address, exc)

    threading.Thread(target=_bulk_refresh, daemon=True).start()
    _log_action(request, 'bulk_redetect', details=f'Re-detecting types for {count} KCOS chassis')
    return JsonResponse({
        'success': True,
        'message': f'Re-detecting chassis types for {count} KCOS chassis in the background.',
    })


# ============================================================
# SUBNET DISCOVERY
# ============================================================

@login_required
def keysight_discover_page(request):
    start_ks_refresh_thread()
    scans = KeysightSubnetScan.objects.all()
    return render(request, 'connect/keysight/subnet_scan.html', {'scans': scans})


@login_required
@require_POST
def keysight_discover_scan(request):
    import ipaddress
    subnet = request.POST.get('subnet', '').strip()
    username = request.POST.get('username', 'admin').strip()
    password = request.POST.get('password', 'admin').strip()

    if not subnet:
        return JsonResponse({'success': False, 'error': 'Subnet is required'})
    try:
        ipaddress.IPv4Network(subnet, strict=False)
    except (ipaddress.AddressValueError, ValueError) as e:
        return JsonResponse({'success': False, 'error': f'Invalid subnet: {e}'})

    scan_id = scan_subnet_async(subnet, username, password)

    scan_obj, _ = KeysightSubnetScan.objects.get_or_create(
        subnet=subnet,
        defaults={'default_username': username, 'default_password': password}
    )
    return JsonResponse({'success': True, 'scan_id': scan_id, 'subnet': subnet})


@login_required
def keysight_discover_scan_status(request, scan_id):
    status = get_scan_status(scan_id)
    if not status:
        return JsonResponse({'success': False, 'error': 'Scan not found'})
    return JsonResponse({'success': True, **status})


@login_required
@require_POST
def keysight_discover_add_devices(request):
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid JSON'})

    discovered = body.get('discovered', [])
    username = body.get('username', 'admin')
    password = body.get('password', 'admin')
    subnet = body.get('subnet', '')

    # Auto-add with KeysightChassis model
    existing_ips = set(KeysightChassis.objects.values_list('ip_address', flat=True))
    added = []
    for d in discovered:
        if d['ip'] in existing_ips or not d.get('auth_ok', False):
            continue
        ch = KeysightChassis.objects.create(
            ip_address=d['ip'], username=username, password=password,
            chassis_type=d.get('chassis_type_guess', 'other'),
        )
        added.append(ch)

    if subnet:
        try:
            scan_obj = KeysightSubnetScan.objects.get(subnet=subnet)
            scan_obj.discovered_count = len(discovered)
            scan_obj.last_scan = timezone.now()
            scan_obj.save()
        except KeysightSubnetScan.DoesNotExist:
            pass

    for ch in added:
        threading.Thread(target=lambda c=ch: (probe_chassis(c), fetch_chassis_data(c)), daemon=True).start()

    return JsonResponse({'success': True, 'added_count': len(added),
                         'added': [{'ip': ch.ip_address, 'id': ch.id} for ch in added]})


@login_required
@require_POST
def keysight_discover_configure(request):
    subnet = request.POST.get('subnet', '').strip()
    auto_scan = request.POST.get('auto_scan', 'false') == 'true'
    try:
        scan_interval = int(request.POST.get('scan_interval', '60'))
    except ValueError:
        scan_interval = 60
    if not subnet:
        return JsonResponse({'success': False, 'error': 'Subnet required'})
    scan_obj, created = KeysightSubnetScan.objects.get_or_create(
        subnet=subnet, defaults={'auto_scan': auto_scan, 'scan_interval': scan_interval})
    if not created:
        scan_obj.auto_scan = auto_scan
        scan_obj.scan_interval = scan_interval
        scan_obj.save()
    return JsonResponse({'success': True, 'auto_scan': auto_scan})


@login_required
@require_POST
def keysight_discover_delete(request, scan_id):
    scan_obj = get_object_or_404(KeysightSubnetScan, id=scan_id)
    scan_obj.delete()
    return JsonResponse({'success': True})


# ============================================================
# HARDWARE INVENTORY
# ============================================================

@login_required
def keysight_hardware_inventory(request):
    from collections import Counter
    from .keysight_aps_generations import aps_generation_chip_stats
    from .keysight_dashboard_filters import (
        build_keysight_filter_chip_context,
        keysight_filtered_chassis_list,
        parse_keysight_filter_params,
    )

    from .keysight_node_associations import get_cached_node_associations
    from .keysight_hw_errors import apply_hw_flags_to_cards, count_hw_error_units, enrich_assoc_hw_flags

    start_ks_refresh_thread()
    f = parse_keysight_filter_params(request)
    q = f['q'].lower()

    view_f = request.GET.get('view', '')
    type_filter = request.GET.get('type', '')
    chassis_filter = request.GET.get('chassis', '')
    all_chassis_qs = KeysightChassis.objects.all()
    kcos_chassis = list(all_chassis_qs.filter(chassis_type__in=KCOS_TYPES))
    node_by_id = get_cached_node_associations([ch.id for ch in kcos_chassis])
    enrich_assoc_hw_flags(node_by_id, [ch.id for ch in kcos_chassis])
    chip_ctx = build_keysight_filter_chip_context(
        request,
        all_chassis_qs,
        preserve_query={
            'view': view_f,
            'type': type_filter,
            'chassis': chassis_filter,
        },
    )
    filtered_chassis = keysight_filtered_chassis_list(
        request, online_only=True, node_by_id=node_by_id,
    )
    filtered_ids = {ch.id for ch in filtered_chassis}

    # Populate shared cache for any online chassis not yet refreshed by background worker.
    fetched, cache_missing = _ensure_chassis_data_cached(filtered_chassis)

    chassis_hw_flags = {
        ch.id: {
            'hardware_error_reported': ch.hardware_error_reported,
            'notes': ch.notes or '',
        }
        for ch in KeysightChassis.objects.filter(id__in=filtered_ids)
    }

    inventory = []
    for ch in KeysightChassis.objects.filter(status='online'):
        if ch.id not in filtered_ids:
            continue
        if chassis_filter and str(ch.id) != chassis_filter:
            continue
        cached = _get_cached(ch.id)
        if not cached:
            continue
        is_kcos = _is_kcos(ch)
        team_tags_str  = ch.team_tags
        team_tags_list = ch.team_tags_list
        ch_fqdn = _reverse_dns(ch.ip_address)
        ch_detail_url = f'/keysight/chassis/{ch.id}/'
        ch_mgmt = ch.mgmt_display
        hw_flag = chassis_hw_flags.get(ch.id, {})
        flagged_cards = apply_hw_flags_to_cards(cached.get('cards', []), ch.id)
        for card in flagged_cards:
            node_flagged = card.get('hardware_error_reported', False)
            ch_flagged = hw_flag.get('hardware_error_reported', False)
            row = {
                'chassis_id': ch.id, 'chassis_ip': ch.mgmt_display or ch.ip_address,
                'chassis_mgmt': ch_mgmt,
                'chassis_detail_url': ch_detail_url,
                'chassis_hostname': ch.hostname or ch.ip_address,
                'chassis_fqdn': ch_fqdn,
                'chassis_type': ch.get_chassis_type_display(),
                'chassis_kcos_version': ch.kcos_version if is_kcos else '',
                'team_tags': team_tags_str,
                'team_tags_list': team_tags_list,
                'hardware_error_reported': node_flagged or ch_flagged,
                'chassis_notes': card.get('hardware_error_notes') or hw_flag.get('notes', ''),
                'node_name': card.get('node_name', ''),
                'item_type': 'card', 'slot': card.get('card_number', ''),
                'port': '', 'model': card.get('type', ''),
                'serial': card.get('serial_number', ''), 'speed': '',
                'link_state': '', 'owner': '', 'state': card.get('state', ''),
                'transceiver_mfg': '', 'transceiver_model': '', 'transceiver_serial': '',
            }
            if not type_filter or type_filter == 'card':
                if not q or q in str(row).lower():
                    inventory.append(row)
        for port in cached.get('ports', []):
            has_xcvr = bool(port.get('transceiver_model', ''))
            row = {
                'chassis_id': ch.id, 'chassis_ip': ch.mgmt_display or ch.ip_address,
                'chassis_mgmt': ch_mgmt,
                'chassis_detail_url': ch_detail_url,
                'chassis_hostname': ch.hostname or ch.ip_address,
                'chassis_fqdn': ch_fqdn,
                'chassis_type': ch.get_chassis_type_display(),
                'chassis_kcos_version': ch.kcos_version if is_kcos else '',
                'team_tags': team_tags_str,
                'team_tags_list': team_tags_list,
                'hardware_error_reported': hw_flag.get('hardware_error_reported', False),
                'chassis_notes': hw_flag.get('notes', ''),
                'item_type': 'port', 'slot': port.get('card_number', ''),
                'port': port.get('port_number', ''), 'model': port.get('type', ''),
                'serial': port.get('transceiver_serial', '') if is_kcos else '',
                'speed': port.get('speed', ''), 'link_state': port.get('link_state', ''),
                'owner': port.get('owner', ''), 'state': port.get('link_state', ''),
                'transceiver_mfg': port.get('transceiver_mfg', ''),
                'transceiver_model': port.get('transceiver_model', ''),
                'transceiver_serial': port.get('transceiver_serial', '') if is_kcos else '',
            }
            if type_filter == 'transceiver':
                if has_xcvr and (not q or q in str(row).lower()):
                    row['item_type'] = 'transceiver'
                    inventory.append(row)
            elif not type_filter or type_filter == 'port':
                if not q or q in str(row).lower():
                    inventory.append(row)

    # View filter: show only reserved (owner != Free) when view=reserved
    if view_f == 'reserved':
        inventory = [r for r in inventory if r.get('owner') and str(r.get('owner', '')).strip() and str(r.get('owner', '')).strip() != 'Free']

    # Attach LabVault reservation info to each inventory row (for All / Reserved tabs)
    chassis_ids_inventory = list({r['chassis_id'] for r in inventory})
    if chassis_ids_inventory:
        _update_reservation_statuses()
        now = timezone.now()
        res_items = KeysightReservationItem.objects.filter(
            chassis_id__in=chassis_ids_inventory,
            reservation__status__in=['active', 'upcoming'],
            reservation__end_time__gte=now,
        ).select_related('reservation', 'reservation__user')
        for row in inventory:
            row['reservation_info'] = []
        for item in res_items:
            r = item.reservation
            user_display = r.user.get_full_name() or r.user.username
            end_str = r.end_time.strftime('%b %d, %H:%M') if r.end_time else ''
            info = {
                'title': r.title or 'Reservation',
                'user': user_display,
                'end_time': end_str,
                'status': getattr(r, 'computed_status', r.status),
            }
            for row in inventory:
                if row['chassis_id'] != item.chassis_id:
                    continue
                try:
                    row_slot = int(row['slot']) if row.get('slot') is not None and str(row.get('slot', '')).strip() != '' else None
                except (TypeError, ValueError):
                    row_slot = None
                try:
                    row_port = int(row['port']) if row.get('port') is not None and str(row.get('port', '')).strip() != '' else None
                except (TypeError, ValueError):
                    row_port = None
                slot_ok = item.slot_number is None or item.slot_number == row_slot
                port_ok = item.port_number is None or item.port_number == row_port
                if slot_ok and port_ok:
                    row['reservation_info'].append(info)

    # When view=reservations, build list of current/future reservation items for the reservations tab
    reservation_rows = []
    if view_f == 'reservations':
        _update_reservation_statuses()
        now = timezone.now()
        items = KeysightReservationItem.objects.filter(
            reservation__status__in=['active', 'upcoming'],
            reservation__end_time__gte=now,
        ).select_related('reservation', 'reservation__user', 'chassis').order_by('reservation__start_time', 'chassis__ip_address')
        for item in items:
            r = item.reservation
            scope_parts = []
            if item.slot_number is not None:
                scope_parts.append(f"Slot {item.slot_number}")
            if item.port_number is not None:
                scope_parts.append(f"Port {item.port_number}")
            scope_str = ' · '.join(scope_parts) if scope_parts else 'Chassis'
            reservation_rows.append({
                'reservation': r,
                'chassis': item.chassis,
                'scope_str': scope_str,
                'status': getattr(r, 'computed_status', r.status),
            })

    total_cards = sum(1 for r in inventory if r['item_type'] == 'card')
    total_ports = sum(1 for r in inventory if r['item_type'] == 'port')
    total_xcvrs = sum(1 for r in inventory if r.get('transceiver_model'))
    hw_error_chassis = count_hw_error_units(filtered_chassis, node_by_id)

    bq = chip_ctx['build_qparams']
    aps_gen_stats = aps_generation_chip_stats(all_chassis_qs, node_by_id)
    aps_gen_stats_urls = [{
        **ag,
        'toggle_url': bq(aps_gen=('' if chip_ctx['aps_gen_filter'] == ag['value'] else ag['value'])),
        'is_selected': chip_ctx['aps_gen_filter'] == ag['value'],
    } for ag in aps_gen_stats]

    context = {
        'inventory': inventory, 'total_cards': total_cards,
        'total_ports': total_ports, 'total_xcvrs': total_xcvrs,
        'total_items': len(inventory), 'search_q': f['q'],
        'type_filter': type_filter, 'chassis_filter': chassis_filter,
        'all_chassis': KeysightChassis.objects.all(),
        'view_filter': view_f,
        'reservation_rows': reservation_rows,
        **chip_ctx,
        'aps_gen_stats': aps_gen_stats_urls,
        'clear_filter_url': bq(
            chassis_type='', aps_gen='', status='', team_tag='', geo='', lab='', q='',
        ),
        'hw_view_filter': view_f,
        'hw_type_filter': type_filter,
        'hw_chassis_filter': chassis_filter,
        'hw_cache_warming': cache_missing > fetched,
        'hw_error_chassis': hw_error_chassis,
    }

    if request.GET.get('format') == 'csv':
        import csv
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="keysight_hw_inventory.csv"'
        writer = csv.writer(response)
        writer.writerow(['Chassis', 'IP', 'Type', 'Item', 'Slot', 'Port',
                         'Model', 'Serial', 'Speed', 'Link', 'Owner', 'State', 'Team Tags'])
        for row in inventory:
            writer.writerow([row['chassis_hostname'], row['chassis_ip'], row['chassis_type'],
                             row['item_type'], row['slot'], row['port'], row['model'],
                             row['serial'], row['speed'], row['link_state'], row['owner'],
                             row['state'], row.get('team_tags', '')])
        return response

    return render(request, 'connect/keysight/hardware_inventory.html', context)


# ------------------------------------------------------------------
# Node Inventory (Mgmt + Compute nodes, BMC, firmware/FRU)
# ------------------------------------------------------------------


def _upsert_bmc_endpoints(nodes: list[dict]):
    """Cache BMC endpoints from node inventory data for use by BMC Board."""
    from .keysight_aps_generations import aps_gen_from_fru, node_aps_generation

    for n in nodes:
        bmc_hostname = n.get('bmc_hostname', '').strip()
        bmc_ip = n.get('bmc_ip', '').strip()
        chassis_id = n.get('chassis_id')
        node_name = n.get('node_name', '')
        serial = (n.get('serial_number', '') or '').strip()
        op_mode = (n.get('operating_mode', '') or 'unknown').strip()
        fru_board_product = (n.get('fru_board_product', '') or '').strip()
        fru_product_name = (n.get('fru_product_name', '') or '').strip()
        aps_gen = node_aps_generation(
            node_name,
            n.get('role', ''),
            chassis_hostname=n.get('chassis_hostname', ''),
            fru_board_product=fru_board_product,
            fru_product_name=fru_product_name,
            aps_gen_hint=n.get('aps_gen', ''),
        ) or aps_gen_from_fru(fru_board_product, fru_product_name) or ''
        valid_modes = {'unknown', 'standalone_merged', 'chassis_mgmt', 'chassis_cn'}
        if op_mode not in valid_modes:
            op_mode = 'unknown'
        if not bmc_hostname:
            continue
        try:
            existing = KeysightBmcEndpoint.objects.filter(hostname=bmc_hostname).first()
            relocated_from_id = None
            relocated_at = None
            if existing:
                if chassis_id and existing.chassis_id and existing.chassis_id != chassis_id:
                    relocated_from_id = existing.chassis_id
                    relocated_at = timezone.now()
                elif existing.relocated_from_chassis_id:
                    relocated_from_id = existing.relocated_from_chassis_id
                    relocated_at = existing.relocated_at
            elif serial:
                dup = KeysightBmcEndpoint.objects.filter(
                    serial_number=serial,
                ).exclude(hostname=bmc_hostname).first()
                if dup and dup.chassis_id and chassis_id and dup.chassis_id != chassis_id:
                    relocated_from_id = dup.chassis_id
                    relocated_at = timezone.now()

            KeysightBmcEndpoint.objects.update_or_create(
                hostname=bmc_hostname,
                defaults={
                    'ip_address': bmc_ip,
                    'source': 'chassis_derived',
                    'chassis_id': chassis_id,
                    'node_name': node_name,
                    'serial_number': serial,
                    'operating_mode': op_mode,
                    'aps_gen': aps_gen if aps_gen in ('10', '15') else '',
                    'fru_board_product': fru_board_product,
                    'relocated_from_chassis_id': relocated_from_id,
                    'relocated_at': relocated_at,
                    'last_seen': timezone.now() if bmc_ip else None,
                },
            )
        except Exception:
            logger.debug('Failed to upsert BMC endpoint for %s', bmc_hostname, exc_info=True)


@login_required
def keysight_node_inventory(request):
    """Unified node inventory: pull mgmt + compute nodes from all KCOS chassis
    with BMC details, serial numbers, firmware/FRU information."""
    from collections import Counter
    start_ks_refresh_thread()
    chassis_filter = request.GET.get('chassis', '')
    q              = request.GET.get('q', '').strip().lower()
    team_tag_f     = request.GET.get('team_tag', '')
    team_tags_selected = [t.strip() for t in team_tag_f.split(',') if t.strip()]

    # Only KCOS chassis have node introspection APIs
    qs = KeysightChassis.objects.filter(
        chassis_type__in=list(KCOS_TYPES),
        status='online',
    )
    if chassis_filter:
        qs = qs.filter(id=chassis_filter)

    # Build tag stats from all KCOS online chassis (before tag filter)
    tag_counter: Counter = Counter()
    for ch in qs:
        for t in ch.team_tags_list:
            tag_counter[t] += 1
    tag_stats_raw = [{'value': t, 'count': c} for t, c in sorted(tag_counter.items())]

    # Apply team tag filter to chassis (OR logic for multiple tags)
    if team_tags_selected:
        qs = [ch for ch in qs if any(t in ch.team_tags_list for t in team_tags_selected)]
    else:
        qs = list(qs)

    # Toggle URLs for multi-select team chips
    from urllib.parse import quote as url_quote
    tag_stats = []
    for ts in tag_stats_raw:
        t = ts['value']
        if t in team_tags_selected:
            new_sel = [x for x in team_tags_selected if x != t]
        else:
            new_sel = team_tags_selected + [t]
        qparams = []
        if chassis_filter:
            qparams.append(f'chassis={chassis_filter}')
        if new_sel:
            qparams.append('team_tag=' + ','.join(new_sel))
        if q:
            qparams.append('q=' + url_quote(q))
        tag_stats.append({
            'value': t, 'count': ts['count'],
            'is_selected': t in team_tags_selected,
            'toggle_url': ('?' + '&'.join(qparams)) if qparams else request.path,
        })

    all_nodes = []
    chassis_errors = []

    # Build a lookup: chassis_id -> team_tags_str for propagation into node dicts
    chassis_tags_map: dict[int, str] = {}

    def _fetch_one(ch):
        """Fetch node inventory for a single chassis."""
        try:
            from .keysight_drivers.kcos import KCOSDriver
            drv = KCOSDriver(ch.ip_address, ch.username, ch.password)
            result = drv.get_node_inventory()
            if result.success and isinstance(result.data, list):
                ch_fqdn = _reverse_dns(ch.ip_address)
                for node in result.data:
                    node['chassis_id'] = ch.id
                    node['chassis_hostname'] = ch.hostname or ch.ip_address
                    node['chassis_fqdn'] = ch_fqdn
                    node['chassis_ip'] = ch.ip_address
                    node['chassis_type'] = ch.get_chassis_type_display()
                    node['team_tags']      = ch.team_tags
                    node['team_tags_list'] = ch.team_tags_list
                return result.data
            else:
                return {'error': result.error, 'chassis': ch.hostname or ch.ip_address}
        except Exception as exc:
            return {'error': str(exc), 'chassis': ch.hostname or ch.ip_address}

    # Fetch concurrently from all chassis
    chassis_list = list(qs)
    if chassis_list:
        with ThreadPoolExecutor(max_workers=min(len(chassis_list), 6)) as pool:
            futures = {pool.submit(_fetch_one, ch): ch for ch in chassis_list}
            for future in futures:
                result = future.result()
                if isinstance(result, list):
                    all_nodes.extend(result)
                elif isinstance(result, dict) and 'error' in result:
                    chassis_errors.append(result)

    # Populate BMC endpoint cache for BMC Board
    _upsert_bmc_endpoints(all_nodes)

    # Apply search filter
    if q:
        all_nodes = [n for n in all_nodes if q in json.dumps(n).lower()]

    # Compute stats
    total_mgmt    = sum(1 for n in all_nodes if n.get('role') == 'Management')
    total_compute = sum(1 for n in all_nodes if n.get('role') == 'Compute')
    total_online  = sum(1 for n in all_nodes if n.get('status') == 'Ready')
    total_bmcs    = sum(1 for n in all_nodes if n.get('bmc_ip'))

    context = {
        'nodes': all_nodes,
        'total_mgmt': total_mgmt,
        'total_compute': total_compute,
        'total_online': total_online,
        'total_bmcs': total_bmcs,
        'total_nodes': len(all_nodes),
        'chassis_errors': chassis_errors,
        'search_q': request.GET.get('q', ''),
        'chassis_filter': chassis_filter,
        'all_chassis': KeysightChassis.objects.filter(
            chassis_type__in=list(KCOS_TYPES)),
        'team_tag_filter': team_tag_f,
        'team_tags_selected': team_tags_selected,
        'tag_stats': tag_stats,
    }

    # CSV export
    if request.GET.get('format') == 'csv':
        import csv
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="keysight_node_inventory.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Chassis', 'Chassis IP', 'Chassis Type', 'Node Name', 'Role',
            'Status', 'Serial Number', 'Internal IP', 'BMC IP', 'BMC Power',
            'BIOS Version', 'BMC Firmware', 'OS Image', 'Kernel', 'K8s Version',
            'NICs', 'SSDs',
        ])
        for n in all_nodes:
            nics_str = '; '.join(f"{nic['name']} v{nic['version']}" for nic in n.get('nics', []))
            ssds_str = '; '.join(f"{ssd['name']} v{ssd['version']}" for ssd in n.get('ssds', []))
            writer.writerow([
                n.get('chassis_hostname', ''), n.get('chassis_ip', ''),
                n.get('chassis_type', ''), n.get('node_name', ''),
                n.get('role', ''), n.get('status', ''),
                n.get('serial_number', ''), n.get('internal_ip', ''),
                n.get('bmc_ip', ''), n.get('bmc_power', ''),
                n.get('bios_version', ''), n.get('bmc_firmware', ''),
                n.get('os_image', ''), n.get('kernel_version', ''),
                n.get('k8s_version', ''), nics_str, ssds_str,
            ])
        return response

    return render(request, 'connect/keysight/node_inventory.html', context)


# ============================================================
# SNAPSHOT / UPGRADE OPERATIONS
# ============================================================

@login_required
def keysight_snapshots_list(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    if not _is_kcos(ch):
        return JsonResponse({'success': False, 'error': 'Snapshots only for KCOS'})
    driver = get_driver(ch)
    result = driver.get_snapshots()
    return JsonResponse({'success': result.success, 'snapshots': result.data if result.success else [], 'error': result.error})


@login_required
@require_POST
def keysight_snapshot_create(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    if not _is_kcos(ch):
        return JsonResponse({'success': False, 'error': 'Snapshots only for KCOS'})
    driver = get_driver(ch)
    label = request.POST.get('label', '').strip()
    if not label:
        return JsonResponse({'success': False, 'error': 'Label required'})
    result = driver.create_snapshot(label)
    return JsonResponse({'success': result.success, 'data': result.data if result.success else None, 'error': result.error})


@login_required
@require_POST
def keysight_snapshot_restore(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    if not _is_kcos(ch):
        return JsonResponse({'success': False, 'error': 'Snapshots only for KCOS'})
    driver = get_driver(ch)
    name = request.POST.get('name', '').strip()
    if not name:
        return JsonResponse({'success': False, 'error': 'Name required'})
    result = driver.restore_snapshot(name)
    if result.success:
        _log_chassis_operation(
            request,
            event_type='downgrade',
            ch=ch,
            detail=f'Snapshot restore: {name}',
            old_value=ch.kcos_version or '',
            new_value=name,
            extra_json={'operation': 'snapshot_restore', 'snapshot': name},
        )
    return JsonResponse({'success': result.success, 'error': result.error})


@login_required
@require_POST
def keysight_snapshot_delete(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    if not _is_kcos(ch):
        return JsonResponse({'success': False, 'error': 'Snapshots only for KCOS'})
    driver = get_driver(ch)
    name = request.POST.get('name', '').strip()
    if not name:
        return JsonResponse({'success': False, 'error': 'Name required'})
    result = driver.delete_snapshot(name)
    return JsonResponse({'success': result.success, 'error': result.error})


@login_required
@require_POST
def keysight_upgrade(request, chassis_id):
    from .request_audit import classify_version_change

    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    driver = get_driver(ch)
    is_kcos = _is_kcos(ch)
    old_v = ch.kcos_version or ''
    new_v = ''
    if is_kcos:
        try:
            components = json.loads(request.body) if request.content_type == 'application/json' else []
        except (json.JSONDecodeError, ValueError):
            components = []
        result = driver.upgrade_firmware(components)
        if components and isinstance(components, list):
            for comp in components:
                if isinstance(comp, dict) and comp.get('version'):
                    new_v = str(comp['version'])
                    break
    else:
        version = request.POST.get('version', '').strip()
        if not version:
            return JsonResponse({'success': False, 'error': 'Version required'})
        new_v = version
        result = driver.upgrade_chassis(version)
    if result.success:
        event_type = classify_version_change(old_v, new_v)
        if event_type == 'deploy_start':
            event_type = 'upgrade'
        _log_chassis_operation(
            request,
            event_type=event_type,
            ch=ch,
            detail='KCOS firmware upgrade' if is_kcos else f'Chassis upgrade to {new_v}',
            old_value=old_v,
            new_value=new_v,
            extra_json={'operation': 'keysight_upgrade', 'kcos': is_kcos},
        )
    return JsonResponse({'success': result.success, 'data': result.data if result.success else None, 'error': result.error})


@login_required
def keysight_upgrade_status(request, chassis_id):
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    driver = get_driver(ch)
    is_kcos = _is_kcos(ch)
    result = driver.get_firmware_upgrade_status() if is_kcos else driver.get_chassis_operations()
    return JsonResponse({'success': result.success, 'data': result.data if result.success else None, 'error': result.error})


# ============================================================
# DEPLOYMENT / UPGRADE CONSOLE
# ============================================================

# Chart names for each package type (online Helm chart lookup)
_CHART_NAMES = {
    'kcos': ['aps-kcos', 'aps-kcos-400'],
    'bps': ['aps-bps'],
    'ixload': ['aps-ixload', 'ixload-eagle'],
    'ixload-drv': ['ixload-driver-package'],
    'cyperf': ['cyperf-agent', 'cyperf-eagle'],
    'cyperf-ati': ['cyperf-ati-update', 'cyperf-ati'],
    'cyperf-ctrl': ['cyperf-controller'],
}

# Map chassis sub-types to the right KCOS chart name
def _kcos_chart_for_chassis(chassis):
    if chassis.chassis_type == 'aps_m8400':
        return 'aps-kcos-400'
    return 'aps-kcos'


@login_required
def keysight_deploy_page(request):
    """Render the deployment management page."""
    chassis_list = KeysightChassis.objects.filter(
        chassis_type__in=list(KCOS_TYPES)
    ).order_by('hostname')
    # Enrich with cached version info
    enriched = []
    for ch in chassis_list:
        cached = _get_cached(ch.id) or {}
        apps = cached.get('deployed_apps', [])
        versions = {}
        for app in apps:
            cn = (app.get('chart_name') or '').lower()
            ver = app.get('chart_version_display') or app.get('chart_version', '')
            if 'kcos' in cn:
                versions['kcos'] = ver
            elif 'bps' in cn:
                versions['bps'] = ver
            elif 'ixload' in cn:
                versions['ixload'] = ver
            elif 'cyperf' in cn:
                versions['cyperf'] = ver
        enriched.append({'chassis': ch, 'versions': versions})

    recent_jobs = KeysightDeploymentJob.objects.select_related('chassis', 'started_by')[:50]
    return render(request, 'connect/keysight/deploy.html', {
        'chassis_list': enriched,
        'recent_jobs': recent_jobs,
    })


@login_required
def keysight_deploy_refresh_builds(request):
    """AJAX: Refresh installed builds for chassis listed on the deploy page.

    Accepts GET with optional ?ids=1,2,3 to limit to specific chassis.
    Fetches get_deployment_info() from each chassis in parallel and returns
    a mapping of chassis_id -> {kcos, bps, ixload, cyperf} version strings.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ids_param = request.GET.get('ids', '')
    if ids_param:
        chassis_ids = [int(x) for x in ids_param.split(',') if x.strip().isdigit()]
        chassis_qs = KeysightChassis.objects.filter(
            id__in=chassis_ids, chassis_type__in=list(KCOS_TYPES))
    else:
        chassis_qs = KeysightChassis.objects.filter(
            chassis_type__in=list(KCOS_TYPES))

    def _fetch_one(ch):
        try:
            drv = get_driver(ch)
            result = drv.get_deployment_info()
            if not result.success:
                return ch.id, {}
            apps = result.data.get('installed_apps', []) if isinstance(result.data, dict) else []
            versions = {}
            for app in apps:
                cn = (app.get('chart_name') or '').lower()
                ver = app.get('chart_version_display') or app.get('chart_version', '')
                if 'kcos' in cn and 'kcos' not in versions:
                    versions['kcos'] = ver
                elif 'bps' in cn and 'bps' not in versions:
                    versions['bps'] = ver
                elif 'ixload' in cn and 'ixload' not in versions:
                    versions['ixload'] = ver
                elif 'cyperf' in cn and 'cyperf' not in versions:
                    versions['cyperf'] = ver
            return ch.id, versions
        except Exception as exc:
            logger.warning('Refresh builds failed for chassis %s: %s', ch.ip_address, exc)
            return ch.id, {}

    results = {}
    chassis_list = list(chassis_qs)
    with ThreadPoolExecutor(max_workers=min(len(chassis_list), 10)) as pool:
        futures = {pool.submit(_fetch_one, ch): ch for ch in chassis_list}
        for future in as_completed(futures):
            try:
                cid, versions = future.result(timeout=45)
                results[str(cid)] = versions
            except Exception:
                ch = futures[future]
                results[str(ch.id)] = {}

    return JsonResponse({'success': True, 'builds': results})


def _parse_version_tuple(v: str):
    """Parse a version string into a comparable tuple.

    Handles formats like '26.1.44', '11.20.325', '9.25.53+20220923.214823.c32f35f7'.
    Returns (major, minor, patch, rest) where rest is the +suffix string.
    """
    # Strip everything after '+' for the numeric part
    base = v.split('+')[0]
    parts = base.split('.')
    nums = []
    for p in parts[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def _get_installed_version(chassis_id: int, pkg_type: str) -> str:
    """Get the currently installed version for a given package type."""
    cached = _get_cached(chassis_id) or {}
    apps = cached.get('deployed_apps', [])
    for app in apps:
        cn = (app.get('chart_name') or '').lower()
        ver = app.get('chart_version', '') or app.get('chart_version_display', '')
        if pkg_type == 'kcos' and 'kcos' in cn and 'ati' not in cn:
            return ver
        elif pkg_type == 'bps' and ('bps' in cn or 'aps-bps' in cn):
            return ver
        elif pkg_type == 'ixload' and 'ixload' in cn:
            return ver
        elif pkg_type == 'cyperf' and 'cyperf' in cn and 'ati' not in cn:
            return ver
    return ''


@login_required
def keysight_deploy_available_versions(request, chassis_id):
    """AJAX: return available online versions for a chart type.

    Filtering strategy:
    - GA/released builds (have displayVersion set): always shown
    - EB/engineering builds (no displayVersion): only shown if they belong to
      the same branch (major.minor) as the currently installed version AND are
      newer than the installed patch level.
    - If no installed version is found, only GA builds are shown.
    - Results limited to 50 most recent entries.
    """
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    pkg_type = request.GET.get('package', 'kcos')
    installed_ver = request.GET.get('installed', '')  # optional hint from frontend
    driver = get_driver(ch)

    # Build list of chart names to try (primary first)
    if pkg_type == 'kcos':
        primary = _kcos_chart_for_chassis(ch)
        fallback = 'aps-kcos' if primary == 'aps-kcos-400' else 'aps-kcos-400'
        chart_candidates = [primary, fallback]
    else:
        charts = _CHART_NAMES.get(pkg_type, [])
        chart_candidates = charts if charts else [pkg_type]

    # Determine installed version (from query param or cache)
    if not installed_ver:
        installed_ver = _get_installed_version(ch.id, pkg_type)

    installed_tuple = _parse_version_tuple(installed_ver) if installed_ver else (0, 0, 0)
    installed_branch = (installed_tuple[0], installed_tuple[1]) if installed_ver else None

    raw_entries = []
    used_chart = ''
    error_msg = ''
    for chart_name in chart_candidates:
        result = driver.get_available_updates(chart_name)
        if result.success and isinstance(result.data, list) and result.data:
            used_chart = chart_name
            raw_entries = result.data
            break
        else:
            error_msg = result.error or 'No versions found'

    if not raw_entries:
        return JsonResponse({'success': False, 'error': error_msg or 'No versions available'})

    # Classify and filter
    ga_versions = []
    eb_versions = []
    seen = set()

    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        ver = entry.get('chartVersion', '')
        if not ver or ver in seen:
            continue
        seen.add(ver)

        display_ver = entry.get('displayVersion', '')
        ver_display = entry.get('chartVersionDisplay', ver)
        name = entry.get('chartName', used_chart)
        is_ga = bool(display_ver)  # GA builds have displayVersion set

        item = {
            'version': ver,
            'display': ver_display or ver,
            'chart_name': name,
            'is_ga': is_ga,
        }

        if is_ga:
            ga_versions.append(item)
        else:
            # EB build: only include if on the same branch AND newer than installed
            ver_tuple = _parse_version_tuple(ver)
            ver_branch = (ver_tuple[0], ver_tuple[1])
            if installed_branch and ver_branch == installed_branch:
                if ver_tuple > installed_tuple:
                    eb_versions.append(item)

    # Sort GA by version descending
    ga_versions.sort(key=lambda v: _parse_version_tuple(v['version']), reverse=True)
    # Sort EB by version descending
    eb_versions.sort(key=lambda v: _parse_version_tuple(v['version']), reverse=True)

    # Combine: EB newer builds first, then GA releases
    # Limit EB to 20 most recent, GA to 30 most recent
    versions = eb_versions[:20] + ga_versions[:30]

    if versions:
        return JsonResponse({
            'success': True,
            'versions': versions,
            'chart_name': used_chart,
            'installed_version': installed_ver,
            'ga_count': len(ga_versions),
            'eb_count': len(eb_versions),
        })
    return JsonResponse({'success': False, 'error': 'No matching versions available'})


@login_required
def keysight_deploy_installed(request, chassis_id):
    """AJAX: return currently installed packages."""
    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    cached = _get_cached(ch.id) or {}
    return JsonResponse({'success': True, 'apps': cached.get('deployed_apps', [])})


@login_required
def keysight_deploy_list_packages(request):
    """AJAX: list previously uploaded packages in media/kcos_packages/."""
    import os
    from django.conf import settings as django_settings

    pkg_dir = os.path.join(django_settings.MEDIA_ROOT, 'kcos_packages')
    packages = []
    if os.path.isdir(pkg_dir):
        host = request.get_host()
        scheme = 'https' if request.is_secure() else 'http'
        for fname in sorted(os.listdir(pkg_dir)):
            fpath = os.path.join(pkg_dir, fname)
            if os.path.isfile(fpath) and (fname.endswith('.tar') or fname.endswith('.enc.tar')):
                stat = os.stat(fpath)
                pkg_type = _detect_package_type(fname)
                packages.append({
                    'filename': fname,
                    'size': stat.st_size,
                    'size_display': f'{stat.st_size / 1048576:.1f} MB',
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                    'url': f'{scheme}://{host}{django_settings.MEDIA_URL}kcos_packages/{fname}',
                    'detected_type': pkg_type,
                })
    return JsonResponse({'success': True, 'packages': packages})


def _detect_package_type(filename: str) -> str:
    """Auto-detect deployment package type from filename."""
    fl = filename.lower()
    # Order matters: more specific patterns first
    if 'ixload-driver' in fl or 'ixload_driver' in fl:
        return 'ixload-drv'
    if 'cyperf' in fl:
        if 'ati' in fl:
            return 'cyperf-ati'
        if 'controller' in fl:
            return 'cyperf-ctrl'
        return 'cyperf'
    if 'kcos' in fl:
        return 'kcos'
    if 'bps' in fl or 'breakingpoint' in fl:
        return 'bps'
    if 'ixload' in fl:
        return 'ixload'
    return 'other'


@login_required
@require_POST
def keysight_deploy_upload(request):
    """Handle .enc.tar file upload. Saves to media/kcos_packages/."""
    import os
    from django.conf import settings as django_settings

    uploaded = request.FILES.get('package')
    if not uploaded:
        return JsonResponse({'success': False, 'error': 'No file provided'})

    # Validate extension
    name = uploaded.name
    if not (name.endswith('.enc.tar') or name.endswith('.tar')):
        return JsonResponse({'success': False, 'error': 'File must be .enc.tar or .tar'})

    pkg_dir = os.path.join(django_settings.MEDIA_ROOT, 'kcos_packages')
    os.makedirs(pkg_dir, exist_ok=True)

    dest = os.path.join(pkg_dir, name)
    with open(dest, 'wb+') as f:
        for chunk in uploaded.chunks():
            f.write(chunk)

    # Build the hosted URL — the chassis will pull from here
    host = request.get_host()
    scheme = 'https' if request.is_secure() else 'http'
    hosted_url = f'{scheme}://{host}{django_settings.MEDIA_URL}kcos_packages/{name}'

    detected_type = _detect_package_type(name)

    _log_action(request, 'deploy_upload', details=f'Uploaded package: {name} (detected: {detected_type})')
    return JsonResponse({
        'success': True,
        'filename': name,
        'size': uploaded.size,
        'url': hosted_url,
        'detected_type': detected_type,
    })


@login_required
@require_POST
def keysight_deploy_start(request):
    """Start a deployment job. Accepts JSON:
    {
        "chassis_ids": [1, 2, 3],
        "package_type": "kcos",
        "job_type": "online",
        "target_version": "9.25",
        "package_url": ""  // for offline: the hosted URL
    }
    """
    import uuid
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid JSON'})

    chassis_ids = body.get('chassis_ids', [])
    package_type = body.get('package_type', 'kcos')
    job_type = body.get('job_type', 'online')
    target_version = body.get('target_version', '')
    package_url = body.get('package_url', '')

    if not chassis_ids:
        return JsonResponse({'success': False, 'error': 'No chassis selected'})
    if job_type == 'online' and not target_version:
        return JsonResponse({'success': False, 'error': 'Version required for online deploy'})
    if job_type == 'offline' and not package_url:
        return JsonResponse({'success': False, 'error': 'Package URL required for offline deploy'})

    batch_id = str(uuid.uuid4())[:12]
    jobs = []
    for cid in chassis_ids:
        try:
            ch = KeysightChassis.objects.get(id=cid)
        except KeysightChassis.DoesNotExist:
            continue
        job = KeysightDeploymentJob.objects.create(
            chassis=ch,
            job_type=job_type,
            package_type=package_type,
            target_version=target_version,
            package_url=package_url,
            status='pending',
            batch_id=batch_id,
            started_by=request.user if request.user.is_authenticated else None,
        )
        _apply_deployment_job_actor(job, request)
        _log_deploy_start_changelog(request, job)
        jobs.append(job)

    # Spawn background workers (max 4 concurrent)
    def _run_batch():
        with ThreadPoolExecutor(max_workers=4) as pool:
            pool.map(_run_deployment_job, [j.id for j in jobs])

    threading.Thread(target=_run_batch, daemon=True).start()

    _log_action(request, 'deploy_start',
                details=f'Batch {batch_id}: {package_type} {job_type} '
                        f'v{target_version} on {len(jobs)} chassis')
    return JsonResponse({
        'success': True,
        'batch_id': batch_id,
        'job_ids': [j.id for j in jobs],
        'count': len(jobs),
    })


@login_required
def keysight_deploy_status(request):
    """AJAX: return status of all active/recent deployment jobs."""
    batch_id = request.GET.get('batch_id', '')
    qs = KeysightDeploymentJob.objects.select_related('chassis', 'started_by')
    if batch_id:
        qs = qs.filter(batch_id=batch_id)
    else:
        qs = qs[:50]

    jobs = []
    for j in qs:
        jobs.append({
            'id': j.id,
            'chassis_id': j.chassis_id,
            'chassis_hostname': j.chassis.hostname or j.chassis.ip_address,
            'chassis_ip': j.chassis.ip_address,
            'job_type': j.job_type,
            'package_type': j.get_package_type_display(),
            'target_version': j.target_version,
            'status': j.status,
            'progress': j.progress,
            'message': j.message,
            'batch_id': j.batch_id,
            'started_by': j.started_by.username if j.started_by else '',
            'created_at': j.created_at.isoformat() if j.created_at else '',
            'updated_at': j.updated_at.isoformat() if j.updated_at else '',
        })
    return JsonResponse({'success': True, 'jobs': jobs})


@login_required
@require_POST
def keysight_deploy_cancel(request, job_id):
    """Cancel a deployment job (only if pending/staging)."""
    job = get_object_or_404(KeysightDeploymentJob, id=job_id)
    if job.status in ('pending', 'staging', 'staged'):
        job.status = 'cancelled'
        job.message = 'Cancelled by user'
        job.save(update_fields=['status', 'message', 'updated_at'])
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'error': f'Cannot cancel job in {job.status} state'})


@login_required
@require_POST
def keysight_deploy_retry(request, job_id):
    """Retry a failed/error/cancelled deployment job.

    Creates a new job with the same parameters and launches it in a background thread.
    """
    import uuid
    old_job = get_object_or_404(KeysightDeploymentJob, id=job_id)
    if old_job.status not in ('error', 'cancelled'):
        return JsonResponse({'success': False, 'error': f'Cannot retry job in {old_job.status} state'})

    batch_id = str(uuid.uuid4())[:12]
    new_job = KeysightDeploymentJob.objects.create(
        chassis=old_job.chassis,
        job_type=old_job.job_type,
        package_type=old_job.package_type,
        target_version=old_job.target_version,
        package_url=old_job.package_url if old_job.job_type == 'offline' else '',
        status='pending',
        batch_id=batch_id,
        started_by=request.user if request.user.is_authenticated else None,
    )
    _apply_deployment_job_actor(new_job, request)
    _log_deploy_start_changelog(request, new_job)

    threading.Thread(target=_run_deployment_job, args=(new_job.id,), daemon=True).start()

    _log_action(request, 'deploy_retry',
                details=f'Retry job {old_job.id} -> {new_job.id}: '
                        f'{old_job.package_type} {old_job.job_type} '
                        f'v{old_job.target_version} on {old_job.chassis.ip_address}')
    return JsonResponse({
        'success': True,
        'new_job_id': new_job.id,
        'batch_id': batch_id,
    })


@login_required
@require_POST
def keysight_changelog_record_operation(request):
    """Record APS/XGS update or downgrade from UI or automation (same change log as deploy console).

    JSON body:
      event_type: deploy_start | deploy_done | deploy_failed | upgrade | downgrade
      chassis_id: int (required)
      detail, old_value, new_value: optional strings
      package_type, job_type, job_id, batch_id: optional metadata
      actor_username, ip_address, user_agent: optional overrides for scripted runs
    """
    from .changelog import log_deploy_lifecycle
    from .request_audit import actor_from_body

    allowed = {
        'deploy_start', 'deploy_done', 'deploy_failed',
        'upgrade', 'downgrade', 'config_change',
    }
    try:
        body = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid JSON'})

    event_type = (body.get('event_type') or '').strip()
    if event_type not in allowed:
        return JsonResponse({
            'success': False,
            'error': f'event_type must be one of: {", ".join(sorted(allowed))}',
        })

    chassis_id = body.get('chassis_id')
    if not chassis_id:
        return JsonResponse({'success': False, 'error': 'chassis_id required'})

    ch = get_object_or_404(KeysightChassis, id=chassis_id)
    actor = actor_from_body(body, request)
    extra = {
        k: body[k]
        for k in ('package_type', 'job_type', 'job_id', 'batch_id', 'operation', 'script')
        if body.get(k) is not None
    }
    extra['chassis_type'] = ch.chassis_type
    if actor.get('browser'):
        extra.setdefault('browser', actor['browser'])
    if actor.get('os'):
        extra.setdefault('os', actor['os'])

    row = log_deploy_lifecycle(
        event_type,
        chassis_id=ch.id,
        target_repr=_chassis_target_repr(ch),
        detail=(body.get('detail') or '')[:500],
        old_value=(body.get('old_value') or '')[:500],
        new_value=(body.get('new_value') or '')[:500],
        actor=actor,
        extra_json=extra or None,
    )
    return JsonResponse({
        'success': True,
        'event_id': row.id if row else None,
    })


# ------------------------------------------------------------------
# Background deployment worker
# ------------------------------------------------------------------

# SSH CLI chart names (used by `kcos deployment online-install`)
_SSH_CHART_NAMES = {
    'kcos': 'aps-kcos',               # works for both M1010/M8400
    'bps': 'aps-bps',
    'ixload': 'aps-ixload',
    'ixload-drv': 'ixload-driver-package',
    'cyperf': 'cyperf-agent',
    'cyperf-ati': 'cyperf-ati-update',
    'cyperf-ctrl': 'cyperf-controller',
}


def _ssh_exec(ip: str, username: str, password: str, command: str,
              timeout: int = 3600, min_runtime: int = 30) -> tuple[int, str]:
    """Execute a command via SSH and return (exit_code, output).

    Uses an interactive shell to handle potential prompts (like EULA acceptance).
    Polls for output until the command completes or timeout is reached.

    Args:
        min_runtime: Minimum seconds the command must have been running before
                     idle-exit checks are allowed. Prevents premature exit
                     from welcome banners containing words like "done".
    """
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=username, password=password,
                       timeout=30, look_for_keys=False, allow_agent=False)
        channel = client.invoke_shell(term='vt100', width=200, height=48)
        channel.settimeout(timeout)

        # Wait for initial prompt / welcome banner
        time.sleep(3)
        initial = b''
        while channel.recv_ready():
            initial += channel.recv(4096)

        # Send the command
        channel.send(command + '\n')

        output = ''
        start = time.time()
        idle_count = 0

        # Specific markers that indicate the command has truly completed.
        # These are KCOS deployment-specific strings that won't appear in
        # a welcome banner or initial shell output.
        _DEFINITIVE_COMPLETE = [
            'the deploy operation is complete',
            '100% the deploy operation is complete',
            'deployment result: done',
        ]
        _INITIATED_MARKER = 'the deploy operation is initiated'
        _ERROR_MARKERS = [
            'error: unknown command',
            'command not found',
            'failed to open a local package',
        ]

        while time.time() - start < timeout:
            time.sleep(3)
            elapsed = time.time() - start

            if channel.recv_ready():
                chunk = channel.recv(65536).decode('utf-8', errors='replace')
                output += chunk
                idle_count = 0

                # Auto-accept EULA if prompted
                if 'Do you accept the EULA' in chunk:
                    channel.send('yes\n')
                    logger.info('SSH [%s]: Auto-accepted EULA', ip)

                lower = output.lower()

                # Definitive completion: these strings only appear when
                # a deployment has truly finished end-to-end
                if any(m in lower for m in _DEFINITIVE_COMPLETE):
                    time.sleep(5)  # Let final output flush
                    while channel.recv_ready():
                        output += channel.recv(65536).decode('utf-8', errors='replace')
                    break

                # Immediate errors (command not recognized etc.)
                if any(m in lower for m in _ERROR_MARKERS):
                    break

                # If the deploy was merely initiated, the SSH session will
                # return to the shell prompt. We should exit SSH and let the
                # caller handle REST polling. Only check after min_runtime
                # to avoid matching stale banner text.
                if elapsed >= min_runtime and _INITIATED_MARKER in lower:
                    logger.info('SSH [%s]: deploy initiated marker found after %.0fs',
                                ip, elapsed)
                    time.sleep(3)
                    while channel.recv_ready():
                        output += channel.recv(65536).decode('utf-8', errors='replace')
                    break

            else:
                idle_count += 1

                # Only consider idle-exit after the minimum runtime has passed
                if elapsed >= min_runtime and idle_count > 10 and output:
                    lower = output.lower()
                    # Only break on very specific markers, NOT generic "done"
                    if any(m in lower for m in _DEFINITIVE_COMPLETE):
                        break
                    # If "initiated" appeared and then shell went idle, command
                    # has returned control — safe to exit
                    if _INITIATED_MARKER in lower:
                        logger.info('SSH [%s]: idle after initiated marker, exiting', ip)
                        break

                # Absolute idle limit: no output for ~3 minutes
                if idle_count > 60:  # 60 * 3s = 3 min
                    logger.warning('SSH [%s]: idle timeout (%ds no output)', ip, idle_count * 3)
                    break

        # Send exit to get exit code
        channel.send('echo $?\n')
        time.sleep(2)
        exit_code = 0
        if channel.recv_ready():
            tail = channel.recv(4096).decode('utf-8', errors='replace')
            # Try to parse the last numeric line
            for line in tail.strip().split('\n'):
                line = line.strip()
                if line.isdigit():
                    exit_code = int(line)

        channel.close()
        return exit_code, output
    finally:
        client.close()


def _run_deployment_job(job_id: int):
    """Execute a single deployment job.

    - Online: uses SSH `kcos deployment online-install chart/version`
    - Offline: uses REST API stage-then-deploy workflow
    """
    try:
        job = KeysightDeploymentJob.objects.select_related(
            'chassis', 'started_by',
        ).get(id=job_id)
    except KeysightDeploymentJob.DoesNotExist:
        return

    if job.status == 'cancelled':
        return

    ch = job.chassis
    driver = get_driver(ch)
    terminal_logged = False

    def _update(status=None, progress=None, message=None):
        nonlocal terminal_logged
        fields = ['updated_at']
        if status:
            job.status = status
            fields.append('status')
        if progress is not None:
            job.progress = progress
            fields.append('progress')
        if message is not None:
            job.message = message
            fields.append('message')
        job.save(update_fields=fields)
        if status in ('success', 'error') and not terminal_logged:
            terminal_logged = True
            _log_deploy_terminal_changelog(job, status, message)

    try:
        if job.job_type == 'online':
            _run_online_deploy(job, ch, driver, _update)
        else:
            _run_offline_deploy(job, ch, driver, _update)
    except Exception as exc:
        logger.exception('Deploy job %d failed: %s', job_id, exc)
        try:
            _update(status='error', message=f'Unexpected error: {exc}')
        except Exception:
            pass


def _run_online_deploy(job, ch, driver, _update):
    """Online deploy via SSH + REST API progress monitoring.

    Three-phase approach:
      Phase 1 — SSH initiation: send `kcos deployment online-install` and wait
                for the "deploy operation is initiated" confirmation (~60-120s).
      Phase 2 — REST polling: poll `GET /deploy/status` every 30s for real
                progress until state is SUCCESS or ERROR (up to 60 min).
      Phase 3 — Verification: call `get_deployment_info()` and compare the
                installed chart version with the target to confirm.
    """
    chart = _SSH_CHART_NAMES.get(job.package_type, job.package_type)
    version = job.target_version
    cmd = f'kcos deployment online-install {chart}/{version}'

    # KCOS umbrella requires -r flag for recursive deployment
    if job.package_type == 'kcos':
        cmd += ' -r'

    job.package_url = f'{chart}/{version}'
    job.save(update_fields=['package_url'])

    # ---- Phase 1: SSH initiation ----
    _update(status='deploying', progress=5,
            message=f'Phase 1: Initiating via SSH — {cmd}')
    logger.info('Deploy job %d: online SSH on %s: %s', job.id, ch.ip_address, cmd)

    try:
        exit_code, output = _ssh_exec(
            ch.ip_address, ch.username, ch.password, cmd, timeout=180)
    except Exception as exc:
        logger.exception('Deploy job %d SSH failed: %s', job.id, exc)
        _update(status='error', message=f'SSH connection failed: {exc}')
        return

    # Log the full SSH output for debugging (first 2000 chars)
    clean_output = re.sub(r'\x1b\[[0-9;]*[mGKHJ]', '', output)
    logger.info('Deploy job %d SSH output (%d chars): %s',
                job.id, len(output), clean_output[:2000])

    out_lower = clean_output.lower()

    # Check if the command itself failed immediately (e.g. command not found)
    immediate_errors = ['command not found', 'error: unknown command',
                        'failed to open a local package']
    for marker in immediate_errors:
        if marker in out_lower:
            tail = clean_output[-500:].strip()
            _update(status='error', progress=5,
                    message=f'SSH command failed: {marker}\n{tail}')
            logger.error('Deploy job %d: immediate SSH error: %s', job.id, marker)
            return

    # Check if the deploy completed fully within the SSH session
    # (can happen for small/fast deploys or if version is already installed)
    ssh_complete_markers = [
        'the deploy operation is complete',
        'deployment complete',
    ]
    if any(m in out_lower for m in ssh_complete_markers) and '100%' in clean_output:
        # Full deployment happened in-band — verify and finish
        tail = clean_output[-500:].strip()
        logger.info('Deploy job %d: deployment completed within SSH session', job.id)
        _update(status='deploying', progress=90,
                message='Deployment completed in SSH session, verifying...')
        _online_deploy_verify(job, ch, driver, _update, tail)
        return

    # The deploy was initiated (or at least the command returned).
    # Check for initiation confirmation
    initiated = 'the deploy operation is initiated' in out_lower
    if initiated:
        logger.info('Deploy job %d: deployment initiated, switching to REST polling', job.id)
    else:
        logger.warning('Deploy job %d: no explicit initiation marker in SSH output, '
                       'proceeding to REST polling anyway', job.id)

    _update(status='deploying', progress=10,
            message='Phase 2: Deployment initiated — monitoring progress via REST API...')

    # ---- Phase 2: REST API progress polling ----
    poll_interval = 30     # seconds between polls
    poll_timeout = 3600    # 60 minutes max
    poll_start = time.time()
    last_progress = 10
    consecutive_errors = 0
    max_consecutive_errors = 20  # 20 * 30s = 10 min of failed polls allowed (cluster reboot)

    while time.time() - poll_start < poll_timeout:
        time.sleep(poll_interval)
        try:
            result = driver.get_deploy_status()
        except Exception as exc:
            consecutive_errors += 1
            logger.warning('Deploy job %d: REST poll error (%d/%d): %s',
                           job.id, consecutive_errors, max_consecutive_errors, exc)
            if consecutive_errors >= max_consecutive_errors:
                _update(status='error', progress=last_progress,
                        message=f'Lost connectivity for {max_consecutive_errors * poll_interval}s: {exc}')
                return
            _update(progress=last_progress,
                    message=f'Waiting for cluster to recover... ({consecutive_errors * poll_interval}s)')
            continue

        if not result.success:
            consecutive_errors += 1
            logger.warning('Deploy job %d: REST poll failed (%d/%d): %s',
                           job.id, consecutive_errors, max_consecutive_errors, result.error)
            if consecutive_errors >= max_consecutive_errors:
                _update(status='error', progress=last_progress,
                        message=f'REST polling failed after {max_consecutive_errors} attempts: {result.error}')
                return
            _update(progress=last_progress,
                    message=f'Waiting for cluster to recover... ({consecutive_errors * poll_interval}s)')
            continue

        # Reset error counter on successful poll
        consecutive_errors = 0
        data = result.data if isinstance(result.data, dict) else {}
        state = (data.get('state') or '').upper()
        progress = data.get('progress', 0)
        msg = data.get('message', '')

        # Update progress (ensure it always moves forward)
        if isinstance(progress, (int, float)) and progress > last_progress:
            last_progress = int(progress)

        _update(progress=last_progress,
                message=f'Phase 2: {state} — {progress}% — {msg[:200]}')
        logger.info('Deploy job %d: REST poll — state=%s progress=%s msg=%s',
                     job.id, state, progress, msg[:100])

        if state == 'SUCCESS':
            logger.info('Deploy job %d: REST reports SUCCESS', job.id)
            _update(status='deploying', progress=95,
                    message='Phase 3: Deployment reported SUCCESS — verifying installed version...')
            # Wait a bit for cluster to stabilize before verification
            time.sleep(30)
            _online_deploy_verify(job, ch, driver, _update, msg)
            return

        if state == 'ERROR':
            error_detail = ''
            result_obj = data.get('result', {})
            if isinstance(result_obj, dict):
                err_obj = result_obj.get('error', {})
                if isinstance(err_obj, dict):
                    error_detail = err_obj.get('summary', '') or err_obj.get('error', '')
            _update(status='error', progress=last_progress,
                    message=f'Deployment failed: {state} — {error_detail or msg}')
            logger.error('Deploy job %d: REST reports ERROR: %s', job.id, error_detail or msg)
            return

    # Timeout — polling exceeded 60 minutes
    _update(status='error', progress=last_progress,
            message=f'Deployment timed out after {poll_timeout // 60} min of REST polling')
    logger.error('Deploy job %d: REST polling timed out after %ds', job.id, poll_timeout)


def _online_deploy_verify(job, ch, driver, _update, deploy_msg: str):
    """Phase 3: Post-deploy verification for online deployments.

    Calls get_deployment_info() and checks that the target version is installed.
    """
    try:
        info_result = driver.get_deployment_info()
    except Exception as exc:
        logger.warning('Deploy job %d: verification call failed: %s', job.id, exc)
        _update(status='success', progress=100,
                message=f'Deployment reported complete (verification unavailable: {exc})')
        return

    if not info_result.success:
        _update(status='success', progress=100,
                message=f'Deployment reported complete (could not verify: {info_result.error})')
        return

    info = info_result.data if isinstance(info_result.data, dict) else {}
    installed_apps = info.get('installed_apps', [])

    # Try to find the target package in installed apps
    target_chart = _SSH_CHART_NAMES.get(job.package_type, job.package_type)
    target_version = job.target_version
    found = False
    installed_ver = ''

    for app in installed_apps:
        cn = (app.get('chart_name') or '').lower()
        ver = app.get('chart_version', '') or app.get('chart_version_display', '')
        if target_chart.replace('-', '') in cn.replace('-', ''):
            installed_ver = ver
            if target_version in ver or ver in target_version:
                found = True
                break

    if found:
        _update(status='success', progress=100,
                message=f'Deployment verified: {target_chart} {installed_ver} installed successfully.')
        logger.info('Deploy job %d: VERIFIED — %s %s installed on %s',
                    job.id, target_chart, installed_ver, ch.ip_address)
    else:
        # Version mismatch — deploy may have succeeded for a different package
        # or verification ran too early. Still mark success since REST reported it.
        _update(status='success', progress=100,
                message=f'Deployment reported complete. Installed: {installed_ver or "unknown"} '
                        f'(target: {target_version}). '
                        f'Please verify manually if versions do not match.')
        logger.warning('Deploy job %d: version mismatch — target=%s installed=%s on %s',
                       job.id, target_version, installed_ver, ch.ip_address)


def _run_offline_deploy(job, ch, driver, _update):
    """Offline deploy via REST API: pre-check -> stage -> validate -> deploy -> poll -> verify.

    Incorporates the full deployment pipeline from the KCOS test framework:
    1. Pre-staging check (ensure staging area is clean)
    2. Stage the build from URL
    3. Poll staging progress
    4. Validate staged charts (resolution, deps, rejects)
    5. Deploy staged builds
    6. Poll deployment progress (with resilience to cluster reboots)
    7. Post-deploy verification (installed matches staged)
    """
    pkg_url = job.package_url
    if not pkg_url:
        _update(status='error', message='No package URL for offline deploy')
        return

    # --- 0. Pre-staging check: ensure staging area is clean ---
    _update(status='staging', progress=2, message='Checking staging area is clean...')
    pre_check = driver.get_staging_status()
    if pre_check.success and isinstance(pre_check.data, dict):
        sd = pre_check.data
        has_charts = bool(sd.get('charts'))
        has_deps = bool(sd.get('dependencyCharts'))
        has_images = bool(sd.get('images'))
        if has_charts or has_deps or has_images:
            _update(progress=3,
                    message='Warning: staging area not empty — existing staged builds found. Proceeding anyway...')
            logger.warning('Deploy job %d: staging area not clean on %s', job.id, ch.ip_address)

    # --- 1. Stage the build ---
    _update(progress=5, message='Staging build from URL...')
    logger.info('Deploy job %d: staging %s on %s', job.id, pkg_url, ch.ip_address)

    stage_result = driver.stage_build(pkg_url)
    if not stage_result.success:
        _update(status='error', message=f'Staging failed: {stage_result.error}')
        return

    stage_data = stage_result.data if isinstance(stage_result.data, dict) else {}
    staging_op_url = stage_data.get('url', '')

    # --- 2. Poll staging progress ---
    _update(progress=10, message='Staging in progress...')
    start_time = time.time()
    while True:
        job.refresh_from_db()
        if job.status == 'cancelled':
            return

        if staging_op_url:
            poll_result = driver.get_staging_operation_progress(staging_op_url)
            if poll_result.success and isinstance(poll_result.data, dict):
                state = poll_result.data.get('state', '')
                progress = poll_result.data.get('progress', 0)
                if state == 'SUCCESS':
                    _update(progress=30, message='Staging complete')
                    break
                elif state == 'ERROR':
                    err_msg = poll_result.data.get('message', '')
                    _update(status='error',
                            message=f'Staging failed: {err_msg}')
                    return
                else:
                    pct = 10 + int(progress * 0.2)
                    _update(progress=pct, message=f'Staging: {progress}%')
        else:
            time.sleep(15)
            break

        if time.time() - start_time > 3600:
            _update(status='error', message='Staging timed out (1 hour)')
            return
        time.sleep(10)

    # --- 3. Validate staging (resolution, deps, rejects) ---
    _update(status='staged', progress=35, message='Validating staged builds...')
    staging_status = driver.get_staging_status()
    staged_chart_info = None  # track what we staged for post-deploy verification

    if staging_status.success and isinstance(staging_status.data, dict):
        sd = staging_status.data
        issues = []
        if not sd.get('isResolved', True):
            issues.append('Not resolved')
        if sd.get('missingRequires'):
            issues.append(f"Missing requires: {sd['missingRequires']}")
        if sd.get('brokenReleases'):
            issues.append(f"Broken releases: {sd['brokenReleases']}")
        if sd.get('existingRejects') is not None:
            issues.append(f"Existing rejects: {sd['existingRejects']}")
        if issues:
            _update(status='error',
                    message=f'Staging validation failed: {"; ".join(issues)}')
            return

        # Extract the staged chart info for post-deploy verification
        for chart_entry in sd.get('charts', []):
            cd = chart_entry.get('chart', {}).get('chartDeployment', {})
            if cd.get('visibilityType') == 'PUBLIC' and cd.get('displayName'):
                staged_chart_info = {
                    'chartName': cd.get('chartName', ''),
                    'chartVersion': cd.get('chartVersion', ''),
                    'displayName': cd.get('displayName', ''),
                }
                break

    # --- 4. Deploy staged builds ---
    _update(status='deploying', progress=40, message='Deploying staged builds...')
    logger.info('Deploy job %d: deploying on %s', job.id, ch.ip_address)

    deploy_result = driver.deploy_staged()
    if not deploy_result.success:
        _update(status='error', message=f'Deploy trigger failed: {deploy_result.error}')
        return

    # --- 5. Poll deployment progress ---
    # During deployment the cluster may reboot, causing transient connection errors.
    # We catch all request exceptions and keep polling until success/error/timeout.
    start_time = time.time()
    while True:
        job.refresh_from_db()
        if job.status == 'cancelled':
            return

        try:
            status_result = driver.get_deploy_status()
        except Exception as poll_exc:
            # Cluster may be rebooting — transient failures are expected
            logger.debug('Deploy job %d: poll error (cluster reboot?): %s',
                         job.id, poll_exc)
            _update(progress=min(job.progress + 1, 95),
                    message='Waiting for cluster to recover...')
            if time.time() - start_time > 3600:
                _update(status='error', message='Deployment timed out (1 hour)')
                return
            time.sleep(30)
            continue

        if status_result.success and isinstance(status_result.data, dict):
            sd = status_result.data
            state = sd.get('state', '')
            progress = sd.get('progress', 0)
            msg = sd.get('message', '')

            if state == 'SUCCESS' and progress == 100:
                err = sd.get('result', {}).get('error', {}).get('error', '')
                if err:
                    _update(status='error', progress=100,
                            message=f'Deploy completed with errors: {err}')
                    return

                # --- 6. Post-deploy verification ---
                verify_msg = _verify_post_deploy(driver, staged_chart_info)
                _update(status='success', progress=100,
                        message=f'Deployment completed successfully. {verify_msg}')
                logger.info('Deploy job %d: SUCCESS on %s', job.id, ch.ip_address)
                return
            elif state == 'ERROR':
                _update(status='error', progress=progress,
                        message=f'Deploy error: {msg}')
                return
            else:
                pct = 40 + int(progress * 0.6)
                _update(progress=min(pct, 99),
                        message=f'Deploying: {progress}% {msg}'.strip())
        elif not status_result.success:
            # Non-exception failure (e.g. HTTP error during reboot)
            _update(progress=min(job.progress + 1, 95),
                    message=f'Waiting for cluster... ({status_result.error})')

        if time.time() - start_time > 3600:
            _update(status='error', message='Deployment timed out (1 hour)')
            return
        time.sleep(30)


def _verify_post_deploy(driver, staged_chart_info: dict | None) -> str:
    """Post-deploy verification: check that installed releases match what was staged.

    Returns a human-readable verification message.
    Based on the validation pattern from the KCOS test framework.
    """
    if not staged_chart_info:
        return 'Staged chart info unavailable — skipped post-deploy verification.'

    try:
        # Get the deploy result releases
        deploy_status = driver.get_deploy_status()
        if not deploy_status.success:
            return 'Could not fetch deploy status for verification.'

        sd = deploy_status.data if isinstance(deploy_status.data, dict) else {}
        releases = sd.get('result', {}).get('releases', [])

        # Check if deployed release matches staged
        deployed_match = False
        for rel in releases:
            cd = rel.get('chartDeployment', {})
            if (cd.get('chartName') == staged_chart_info['chartName'] and
                    cd.get('chartVersion') == staged_chart_info['chartVersion']):
                deployed_match = True
                break

        # Also verify against installed builds
        installed_result = driver.get_deployed_apps()
        installed_match = False
        if installed_result.success and isinstance(installed_result.data, list):
            for app in installed_result.data:
                cn = app.get('chart_name', '')
                cv = app.get('chart_version', '')
                if (cn == staged_chart_info['chartName'] and
                        cv == staged_chart_info['chartVersion']):
                    installed_match = True
                    break

        parts = []
        if deployed_match:
            parts.append('Deploy release verified')
        else:
            parts.append('Warning: deployed release mismatch')
        if installed_match:
            parts.append('installed build verified')
        else:
            parts.append('installed build not yet confirmed (may need refresh)')
        return ' | '.join(parts) + '.'
    except Exception as exc:
        return f'Post-deploy verification error: {exc}'


# ============================================================
# AUDIT LOG (keysight-specific)
# ============================================================

@login_required
def keysight_audit_log(request):
    logs = AuditLog.objects.filter(details__startswith='[Keysight]').select_related('user')[:200]
    return render(request, 'connect/keysight/audit_log.html', {'logs': logs})


# ============================================================
# RESERVATION SYSTEM
# ============================================================

def _update_reservation_statuses():
    """Bulk-update reservation statuses based on current time."""
    now = timezone.now()
    KeysightReservation.objects.filter(status='upcoming', start_time__lte=now, end_time__gte=now).update(status='active')
    KeysightReservation.objects.filter(status__in=['upcoming', 'active'], end_time__lt=now).update(status='expired')


@login_required
def keysight_reservations(request):
    """List all reservations with filters."""
    _update_reservation_statuses()

    reservations = KeysightReservation.objects.select_related('user').prefetch_related('items__chassis').all()

    # Filters
    chassis_id = request.GET.get('chassis')
    status = request.GET.get('status')
    user_filter = request.GET.get('user')

    if chassis_id:
        reservations = reservations.filter(items__chassis_id=chassis_id).distinct()
    if status:
        reservations = reservations.filter(status=status)
    if user_filter:
        reservations = reservations.filter(user__username__icontains=user_filter)

    all_chassis = KeysightChassis.objects.all()

    return render(request, 'connect/keysight/reservations.html', {
        'reservations': reservations[:100],
        'all_chassis': all_chassis,
        'filter_chassis': chassis_id,
        'filter_status': status,
        'filter_user': user_filter or '',
    })


@login_required
def keysight_create_reservation(request):
    """Create a new hardware reservation."""
    from django.conf import settings as django_settings
    all_chassis = KeysightChassis.objects.all()
    default_email = ', '.join(getattr(django_settings, 'KEYSIGHT_RESERVATION_EMAIL_RECIPIENTS', []))

    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        start_str = request.POST.get('start_time', '').strip()
        end_str = request.POST.get('end_time', '').strip()
        notification_emails = request.POST.get('notification_emails', '').strip()

        if not title or not start_str or not end_str:
            messages.error(request, 'Title, start time, and end time are required.')
            return render(request, 'connect/keysight/reservation_form.html', {
                'all_chassis': all_chassis, 'editing': False, 'default_email': default_email,
            })

        try:
            start_time = timezone.make_aware(datetime.strptime(start_str, '%Y-%m-%dT%H:%M'))
            end_time = timezone.make_aware(datetime.strptime(end_str, '%Y-%m-%dT%H:%M'))
        except ValueError:
            messages.error(request, 'Invalid date/time format.')
            return render(request, 'connect/keysight/reservation_form.html', {
                'all_chassis': all_chassis, 'editing': False, 'default_email': default_email,
            })

        if end_time <= start_time:
            messages.error(request, 'End time must be after start time.')
            return render(request, 'connect/keysight/reservation_form.html', {
                'all_chassis': all_chassis, 'editing': False, 'default_email': default_email,
            })

        reservation = KeysightReservation.objects.create(
            title=title,
            description=description,
            user=request.user,
            start_time=start_time,
            end_time=end_time,
            notification_emails=notification_emails,
        )

        # Parse items from form: items are sent as groups
        # chassis_X, slot_X, port_X, notes_X  where X is an index
        item_count = 0
        for key in request.POST:
            if key.startswith('chassis_'):
                idx = key.split('_', 1)[1]
                ch_id = request.POST.get(f'chassis_{idx}')
                slot_raw = request.POST.get(f'slot_{idx}', '').strip()
                port_raw = request.POST.get(f'port_{idx}', '').strip()
                notes = request.POST.get(f'notes_{idx}', '').strip()

                if not ch_id:
                    continue

                try:
                    ch = KeysightChassis.objects.get(id=int(ch_id))
                except (KeysightChassis.DoesNotExist, ValueError):
                    continue

                slot_number = int(slot_raw) if slot_raw else None
                port_number = int(port_raw) if port_raw else None

                KeysightReservationItem.objects.create(
                    reservation=reservation,
                    chassis=ch,
                    slot_number=slot_number,
                    port_number=port_number,
                    notes=notes,
                )
                item_count += 1

        if item_count == 0:
            reservation.delete()
            messages.error(request, 'At least one hardware item is required.')
            return render(request, 'connect/keysight/reservation_form.html', {
                'all_chassis': all_chassis, 'editing': False, 'default_email': default_email,
            })

        # Send email notification
        _send_reservation_email(reservation)

        _audit_log(request, 'create',
                   f'[Keysight] Created reservation "{title}" with {item_count} items')
        messages.success(request, f'Reservation "{title}" created successfully.')
        return redirect('keysight_reservations')

    return render(request, 'connect/keysight/reservation_form.html', {
        'all_chassis': all_chassis, 'editing': False, 'default_email': default_email,
    })


@login_required
def keysight_reservation_detail(request, reservation_id):
    """View a single reservation."""
    _update_reservation_statuses()
    reservation = get_object_or_404(
        KeysightReservation.objects.select_related('user').prefetch_related('items__chassis'),
        id=reservation_id,
    )
    return render(request, 'connect/keysight/reservation_detail.html', {
        'reservation': reservation,
    })


@login_required
def keysight_edit_reservation(request, reservation_id):
    """Edit an existing reservation."""
    reservation = get_object_or_404(KeysightReservation, id=reservation_id)
    all_chassis = KeysightChassis.objects.all()

    # Only the creator or superuser can edit
    if reservation.user != request.user and not request.user.is_superuser:
        messages.error(request, 'You can only edit your own reservations.')
        return redirect('keysight_reservation_detail', reservation_id=reservation_id)

    if reservation.status == 'cancelled':
        messages.error(request, 'Cannot edit a cancelled reservation.')
        return redirect('keysight_reservation_detail', reservation_id=reservation_id)

    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        start_str = request.POST.get('start_time', '').strip()
        end_str = request.POST.get('end_time', '').strip()

        if not title or not start_str or not end_str:
            messages.error(request, 'Title, start time, and end time are required.')
            return render(request, 'connect/keysight/reservation_form.html', {
                'all_chassis': all_chassis, 'editing': True, 'reservation': reservation,
            })

        try:
            start_time = timezone.make_aware(datetime.strptime(start_str, '%Y-%m-%dT%H:%M'))
            end_time = timezone.make_aware(datetime.strptime(end_str, '%Y-%m-%dT%H:%M'))
        except ValueError:
            messages.error(request, 'Invalid date/time format.')
            return render(request, 'connect/keysight/reservation_form.html', {
                'all_chassis': all_chassis, 'editing': True, 'reservation': reservation,
            })

        if end_time <= start_time:
            messages.error(request, 'End time must be after start time.')
            return render(request, 'connect/keysight/reservation_form.html', {
                'all_chassis': all_chassis, 'editing': True, 'reservation': reservation,
            })

        reservation.title = title
        reservation.description = description
        reservation.start_time = start_time
        reservation.end_time = end_time
        reservation.notification_emails = request.POST.get('notification_emails', '').strip()
        reservation.save()

        # Replace items
        reservation.items.all().delete()
        item_count = 0
        for key in request.POST:
            if key.startswith('chassis_'):
                idx = key.split('_', 1)[1]
                ch_id = request.POST.get(f'chassis_{idx}')
                slot_raw = request.POST.get(f'slot_{idx}', '').strip()
                port_raw = request.POST.get(f'port_{idx}', '').strip()
                notes = request.POST.get(f'notes_{idx}', '').strip()

                if not ch_id:
                    continue
                try:
                    ch = KeysightChassis.objects.get(id=int(ch_id))
                except (KeysightChassis.DoesNotExist, ValueError):
                    continue

                slot_number = int(slot_raw) if slot_raw else None
                port_number = int(port_raw) if port_raw else None

                KeysightReservationItem.objects.create(
                    reservation=reservation,
                    chassis=ch,
                    slot_number=slot_number,
                    port_number=port_number,
                    notes=notes,
                )
                item_count += 1

        _audit_log(request, 'update',
                   f'[Keysight] Updated reservation "{title}" with {item_count} items')
        messages.success(request, f'Reservation "{title}" updated.')
        return redirect('keysight_reservation_detail', reservation_id=reservation.id)

    return render(request, 'connect/keysight/reservation_form.html', {
        'all_chassis': all_chassis, 'editing': True, 'reservation': reservation,
    })


@login_required
@require_POST
def keysight_cancel_reservation(request, reservation_id):
    """Cancel a reservation."""
    reservation = get_object_or_404(KeysightReservation, id=reservation_id)

    if reservation.user != request.user and not request.user.is_superuser:
        messages.error(request, 'You can only cancel your own reservations.')
        return redirect('keysight_reservation_detail', reservation_id=reservation_id)

    reservation.status = 'cancelled'
    reservation.save()

    _audit_log(request, 'delete',
               f'[Keysight] Cancelled reservation "{reservation.title}"')
    messages.success(request, f'Reservation "{reservation.title}" cancelled.')
    return redirect('keysight_reservations')


@login_required
@require_POST
def keysight_delete_reservation(request, reservation_id):
    """Delete a reservation: cancel if active/upcoming, hard-delete if cancelled/expired."""
    reservation = get_object_or_404(KeysightReservation, id=reservation_id)

    if reservation.user != request.user and not request.user.is_superuser:
        messages.error(request, 'You can only delete your own reservations.')
        return redirect('keysight_reservations')

    title = reservation.title
    if reservation.status in ('active', 'upcoming'):
        reservation.status = 'cancelled'
        reservation.save()
        _audit_log(request, 'delete', f'[Keysight] Cancelled reservation "{title}"')
        messages.success(request, f'Reservation "{title}" cancelled.')
    else:
        reservation.delete()
        _audit_log(request, 'delete', f'[Keysight] Deleted reservation "{title}"')
        messages.success(request, f'Reservation "{title}" deleted.')

    return redirect('keysight_reservations')


@login_required
@require_POST
def keysight_quick_reserve(request):
    """Quick-reserve hardware with minimal input (AJAX endpoint).
    Accepts: chassis_id, slot_number (opt), port_number (opt), duration, notes.
    Auto-fills user, email, and title."""
    from datetime import timedelta

    chassis_id = request.POST.get('chassis_id')
    slot_raw = request.POST.get('slot_number', '').strip()
    port_raw = request.POST.get('port_number', '').strip()
    duration = request.POST.get('duration', '4h').strip()
    notes = request.POST.get('notes', '').strip()
    custom_end = request.POST.get('custom_end', '').strip()

    if not chassis_id:
        return JsonResponse({'ok': False, 'error': 'No chassis selected.'}, status=400)

    try:
        ch = KeysightChassis.objects.get(id=int(chassis_id))
    except (KeysightChassis.DoesNotExist, ValueError):
        return JsonResponse({'ok': False, 'error': 'Chassis not found.'}, status=404)

    now = timezone.now()
    start_time = now

    # Parse duration presets
    duration_map = {
        '1h': timedelta(hours=1),
        '4h': timedelta(hours=4),
        '8h': timedelta(hours=8),
        '24h': timedelta(hours=24),
        '1w': timedelta(weeks=1),
    }
    if duration == 'custom' and custom_end:
        try:
            end_time = timezone.make_aware(datetime.strptime(custom_end, '%Y-%m-%dT%H:%M'))
        except ValueError:
            return JsonResponse({'ok': False, 'error': 'Invalid custom end time.'}, status=400)
    elif duration in duration_map:
        end_time = start_time + duration_map[duration]
    else:
        end_time = start_time + timedelta(hours=4)

    if end_time <= start_time:
        return JsonResponse({'ok': False, 'error': 'End time must be in the future.'}, status=400)

    # Title: use user-provided or auto-generate
    chassis_name = ch.hostname or ch.ip_address
    user_title = request.POST.get('title', '').strip()
    title = user_title if user_title else f"{request.user.username} - {chassis_name} - {now.strftime('%b %d %H:%M')}"
    email = request.user.email or ''

    reservation = KeysightReservation.objects.create(
        title=title,
        description=notes,
        user=request.user,
        start_time=start_time,
        end_time=end_time,
        notification_emails=email,
    )

    slot_number = int(slot_raw) if slot_raw else None
    port_number = int(port_raw) if port_raw else None

    KeysightReservationItem.objects.create(
        reservation=reservation,
        chassis=ch,
        slot_number=slot_number,
        port_number=port_number,
        notes=notes,
    )

    _send_reservation_email(reservation)

    _audit_log(request, 'create', f'[Keysight] Quick-reserved "{title}"')

    level = 'port' if port_number else ('slot' if slot_number else 'chassis')
    return JsonResponse({
        'ok': True,
        'reservation_id': reservation.id,
        'title': title,
        'level': level,
        'end_time': end_time.strftime('%Y-%m-%d %H:%M'),
    })


def _send_reservation_email(reservation):
    """Send email notification about the reservation in a background thread."""
    from django.conf import settings as django_settings

    def _do_send():
        try:
            from django.core.mail import send_mail

            # Use per-reservation emails if set, otherwise fall back to settings
            if reservation.notification_emails and reservation.notification_emails.strip():
                recipients = [e.strip() for e in reservation.notification_emails.split(',') if e.strip()]
            else:
                recipients = getattr(django_settings, 'KEYSIGHT_RESERVATION_EMAIL_RECIPIENTS', []) or []

            items_text = []
            for item in reservation.items.select_related('chassis').all():
                line = f"  - {item.chassis.hostname or item.chassis.ip_address}"
                if item.slot_number is not None:
                    line += f" / Slot {item.slot_number}"
                if item.port_number is not None:
                    line += f" / Port {item.port_number}"
                if item.notes:
                    line += f"  ({item.notes})"
                items_text.append(line)

            body = (
                f"New Keysight Hardware Reservation\n"
                f"{'=' * 40}\n\n"
                f"Title: {reservation.title}\n"
                f"User: {reservation.user.get_full_name() or reservation.user.username}\n"
                f"Start: {reservation.start_time:%Y-%m-%d %H:%M}\n"
                f"End: {reservation.end_time:%Y-%m-%d %H:%M}\n"
                f"Description: {reservation.description or 'N/A'}\n\n"
                f"Reserved Hardware:\n" + '\n'.join(items_text) + '\n\n'
                f"---\nSent by LabVault Reservation System"
            )

            send_mail(
                subject=f'Reservation: {reservation.title} by {reservation.user.username}',
                message=body,
                from_email=getattr(django_settings, 'DEFAULT_FROM_EMAIL', 'netconnect@localhost'),
                recipient_list=recipients,
                fail_silently=True,
            )

            reservation.email_notified = True
            reservation.save(update_fields=['email_notified'])
        except Exception as e:
            logger.warning(f'Failed to send reservation email: {e}')

    thread = threading.Thread(target=_do_send, daemon=True)
    thread.start()


def get_active_reservations_for_chassis(chassis_id):
    """Helper to fetch active/upcoming reservations for a specific chassis.
    Returns a dict with lookup keys for quick badge rendering:
    {
        'chassis_level': [reservation, ...],
        'slot_X': [reservation, ...],
        'slot_X_port_Y': [reservation, ...],
    }
    """
    _update_reservation_statuses()
    now = timezone.now()
    items = KeysightReservationItem.objects.filter(
        chassis_id=chassis_id,
        reservation__status__in=['active', 'upcoming'],
        reservation__end_time__gte=now,
    ).select_related('reservation__user')

    result = {'chassis_level': []}
    for item in items:
        if item.slot_number is None:
            result['chassis_level'].append(item)
        elif item.port_number is None:
            key = f'slot_{item.slot_number}'
            result.setdefault(key, []).append(item)
        else:
            key = f'slot_{item.slot_number}_port_{item.port_number}'
            result.setdefault(key, []).append(item)
    return result


def get_reservations_summaries_for_chassis(chassis_ids):
    """Batch reservation summaries for dashboard lists (one status sync + one query)."""
    if not chassis_ids:
        return {}
    _update_reservation_statuses()
    now = timezone.now()
    items = KeysightReservationItem.objects.filter(
        chassis_id__in=chassis_ids,
        reservation__status__in=['active', 'upcoming'],
        reservation__end_time__gte=now,
    ).select_related('reservation', 'reservation__user').order_by('reservation__start_time')

    lists_by_chassis = {cid: [] for cid in chassis_ids}
    seen = {cid: set() for cid in chassis_ids}
    for item in items:
        cid = item.chassis_id
        if cid not in lists_by_chassis:
            continue
        r = item.reservation
        if r.id in seen[cid]:
            continue
        seen[cid].add(r.id)
        scope_parts = []
        if item.slot_number is not None:
            scope_parts.append(f"Slot {item.slot_number}")
        if item.port_number is not None:
            scope_parts.append(f"Port {item.port_number}")
        scope_str = ' · '.join(scope_parts) if scope_parts else 'Chassis'
        lists_by_chassis[cid].append({
            'reservation': r,
            'scope_str': scope_str,
            'status': getattr(r, 'computed_status', r.status),
        })
    return {
        cid: (len(rows) > 0, rows)
        for cid, rows in lists_by_chassis.items()
    }


def get_reservations_summary_for_chassis(chassis_id):
    """Return current and future reservations for a chassis for dashboard/inventory display.
    Returns: (has_reservations: bool, reservations_list: list of dicts with reservation, scope_str, status).
    """
    has_res, rows = get_reservations_summaries_for_chassis([chassis_id]).get(chassis_id, (False, []))
    return has_res, rows


# ============================================================
# BMC ASSOCIATIONS — mgmt-to-compute node mapping per chassis
# ============================================================

@login_required
def keysight_bmc_associations(request):
    """BMC Associations: broad view mapping each mgmt node to its compute nodes,
    showing which nodes are up/down per chassis."""
    start_ks_refresh_thread()

    chassis_filter = request.GET.get('chassis', '')
    q = request.GET.get('q', '').strip().lower()

    qs = KeysightChassis.objects.filter(
        chassis_type__in=list(KCOS_TYPES),
    )
    if chassis_filter:
        qs = qs.filter(id=chassis_filter)

    chassis_list = list(qs)

    # Fetch node data from all online chassis
    all_results: dict[int, list[dict] | dict] = {}
    online_chassis = [ch for ch in chassis_list if ch.status == 'online']

    if online_chassis:
        with ThreadPoolExecutor(max_workers=min(len(online_chassis), 6)) as pool:
            futures = {pool.submit(_fetch_bmc_data_from_chassis, ch): ch for ch in online_chassis}
            for future in futures:
                ch = futures[future]
                result = future.result()
                all_results[ch.id] = result

    # Build associations per chassis
    associations = []
    total_mgmt = 0
    total_compute = 0
    total_up = 0
    total_down = 0
    total_chassis_online = 0
    total_chassis_offline = 0

    for ch in chassis_list:
        result = all_results.get(ch.id)
        chassis_entry = {
            'chassis': ch,
            'chassis_online': ch.status == 'online',
            'fqdn': _reverse_dns(ch.ip_address),
            'mgmt_node': None,
            'compute_nodes': [],
            'error': None,
        }

        if ch.status != 'online':
            chassis_entry['error'] = f'Chassis {ch.status}'
            total_chassis_offline += 1
        elif isinstance(result, dict) and 'error' in result:
            chassis_entry['error'] = result['error']
            total_chassis_offline += 1
        elif isinstance(result, list):
            total_chassis_online += 1
            for node in result:
                node_entry = {
                    'name': node.get('node_name', ''),
                    'role': node.get('role', ''),
                    'status': node.get('status', ''),
                    'is_ready': node.get('status', '') == 'Ready',
                    'internal_ip': node.get('internal_ip', ''),
                    'bmc_ip': node.get('bmc_ip', ''),
                    'bmc_hostname': node.get('bmc_hostname', ''),
                    'bmc_power': node.get('bmc_power', ''),
                    'serial_number': node.get('serial_number', ''),
                    'bmc_firmware': node.get('bmc_firmware', ''),
                    'os_image': node.get('os_image', ''),
                    'k8s_version': node.get('k8s_version', ''),
                }
                if node.get('role') in ('Management', 'Standalone'):
                    chassis_entry['mgmt_node'] = node_entry
                    total_mgmt += 1
                    if node_entry['is_ready']:
                        total_up += 1
                    else:
                        total_down += 1
                else:
                    chassis_entry['compute_nodes'].append(node_entry)
                    total_compute += 1
                    if node_entry['is_ready']:
                        total_up += 1
                    else:
                        total_down += 1
        else:
            total_chassis_offline += 1

        associations.append(chassis_entry)

    # Apply search filter
    if q:
        filtered = []
        for a in associations:
            haystack = (a['chassis'].hostname or a['chassis'].ip_address).lower()
            if a['mgmt_node']:
                haystack += ' ' + a['mgmt_node']['name'].lower()
            for cn in a['compute_nodes']:
                haystack += ' ' + cn['name'].lower()
            if q in haystack:
                filtered.append(a)
        associations = filtered

    context = {
        'associations': associations,
        'total_chassis': len(associations),
        'total_chassis_online': total_chassis_online,
        'total_chassis_offline': total_chassis_offline,
        'total_mgmt': total_mgmt,
        'total_compute': total_compute,
        'total_up': total_up,
        'total_down': total_down,
        'search_q': request.GET.get('q', ''),
        'chassis_filter': chassis_filter,
        'all_chassis': KeysightChassis.objects.filter(
            chassis_type__in=list(KCOS_TYPES)),
    }

    return render(request, 'connect/keysight/bmc_associations.html', context)


# ============================================================
# DNS helpers
# ============================================================

def _reverse_dns(ip: str) -> str:
    """Reverse DNS lookup; returns FQDN or empty string. Cached in-process."""
    from .hardware_links import reverse_dns_hostname

    return reverse_dns_hostname(ip)


# ============================================================
# BMC BOARD — uses same KCOS REST API as Node Inventory +
#              optional ipmitool enrichment for primary_os_name
# ============================================================

def _resolve_bmc_ip(hostname: str, domain_suffix: str = '') -> str:
    """Try DNS resolution for a BMC hostname, return IP or empty string."""
    candidates = [hostname]
    if domain_suffix and not hostname.endswith(domain_suffix):
        candidates.append(f'{hostname}.{domain_suffix}')
    for name in candidates:
        try:
            return socket.gethostbyname(name)
        except socket.gaierror:
            continue
    return ''


def _get_bmc_credentials(endpoint: KeysightBmcEndpoint | None = None,
                         chassis: KeysightChassis | None = None) -> tuple[str, str]:
    """Return (user, password) for a BMC, cascading: endpoint override -> chassis -> env default."""
    import os
    if endpoint and endpoint.username:
        return endpoint.username, endpoint.password
    if chassis:
        return chassis.username, chassis.password
    default_user = os.environ.get('BMC_DEFAULT_USER', 'admin')
    default_pass = os.environ.get('BMC_DEFAULT_PASS', 'admin')
    return default_user, default_pass


def _fetch_bmc_data_from_chassis(ch: KeysightChassis) -> list[dict] | dict:
    """Fetch BMC/node data from a single KCOS chassis via REST API.

    Uses the same approach as get_node_inventory() — this is how
    Node & BMC Inventory gets its data.  Returns a list of node dicts
    or an error dict.
    """
    try:
        from .keysight_drivers.kcos import KCOSDriver
        drv = KCOSDriver(ch.ip_address, ch.username, ch.password)
        result = drv.get_node_inventory()
        if result.success and isinstance(result.data, list):
            ch_fqdn = _reverse_dns(ch.ip_address)
            for node in result.data:
                node['chassis_id'] = ch.id
                node['chassis_hostname'] = ch.hostname or ch.ip_address
                node['chassis_fqdn'] = ch_fqdn
                node['chassis_ip'] = ch.ip_address
                node['chassis_type'] = ch.get_chassis_type_display()
                node['team_tags'] = ch.team_tags
                node['team_tags_list'] = ch.team_tags_list
            return result.data
        else:
            return {'error': result.error, 'chassis': ch.hostname or ch.ip_address}
    except Exception as exc:
        return {'error': str(exc), 'chassis': ch.hostname or ch.ip_address}


@login_required
def keysight_bmc_board(request):
    """BMC Board: pull BMC data from all KCOS chassis (same as Node Inventory),
    then optionally enrich with direct ipmitool for primary_os_name."""
    import os
    from .bmc_ipmi import fetch_all_bmcs

    start_ks_refresh_thread()
    domain_suffix = (os.environ.get('BMC_DOMAIN_SUFFIX') or '').strip()

    q = request.GET.get('q', '').strip().lower()
    mps_filter = request.GET.get('mps', '').strip()
    status_filter = request.GET.get('status', '')
    chassis_filter = request.GET.get('chassis', '')
    ipmi_enrich = request.GET.get('ipmi', '') == '1'

    # ---- Step 1: Fetch BMC data from all online KCOS chassis (same as Node Inventory) ----
    qs = KeysightChassis.objects.filter(
        chassis_type__in=list(KCOS_TYPES),
        status='online',
    )
    if chassis_filter:
        qs = qs.filter(id=chassis_filter)

    chassis_list = list(qs)
    all_nodes: list[dict] = []
    chassis_errors: list[dict] = []

    if chassis_list:
        with ThreadPoolExecutor(max_workers=min(len(chassis_list), 6)) as pool:
            futures = {pool.submit(_fetch_bmc_data_from_chassis, ch): ch for ch in chassis_list}
            for future in futures:
                result = future.result()
                if isinstance(result, list):
                    all_nodes.extend(result)
                elif isinstance(result, dict) and 'error' in result:
                    chassis_errors.append(result)

    # Cache BMC endpoints for future use
    _upsert_bmc_endpoints(all_nodes)
    bmc_ep_by_hostname = {
        ep.hostname.lower(): ep
        for ep in KeysightBmcEndpoint.objects.select_related(
            'chassis', 'relocated_from_chassis',
        ).all()
    }

    # ---- Step 2: Also include manual-import BMC endpoints not already covered ----
    manual_eps = list(KeysightBmcEndpoint.objects.filter(source='manual_import'))
    kcos_bmc_hostnames = {n.get('bmc_hostname', '').lower() for n in all_nodes if n.get('bmc_hostname')}

    for ep in manual_eps:
        if ep.hostname.lower() in kcos_bmc_hostnames:
            continue
        if not ep.ip_address:
            ep.ip_address = _resolve_bmc_ip(ep.hostname, domain_suffix)
            if ep.ip_address:
                ep.save(update_fields=['ip_address', 'updated_at'])
        all_nodes.append({
            'node_name': ep.node_name or ep.hostname,
            'role': 'Unknown',
            'status': '',
            'internal_ip': '',
            'bmc_name': '',
            'bmc_hostname': ep.hostname,
            'bmc_ip': ep.ip_address,
            'bmc_power': '',
            'serial_number': '',
            'bios_version': '',
            'bmc_firmware': '',
            'bmc_firmware_name': '',
            'chassis_id': ep.chassis_id,
            'chassis_hostname': ep.chassis.hostname if ep.chassis else '',
            'chassis_fqdn': _reverse_dns(ep.chassis.ip_address) if ep.chassis else '',
            'chassis_ip': ep.chassis.ip_address if ep.chassis else '',
            'chassis_type': ep.chassis.get_chassis_type_display() if ep.chassis else '',
            'source': 'manual_import',
        })

    # ---- Step 3: Optional ipmitool enrichment for primary_os_name ----
    ipmi_results: dict[str, dict] = {}
    if ipmi_enrich:
        targets = []
        bmc_ips_for_ipmi = []
        for n in all_nodes:
            bmc_ip = n.get('bmc_ip', '')
            bmc_hostname = n.get('bmc_hostname', '')
            if bmc_ip:
                ch_id = n.get('chassis_id')
                ch = None
                if ch_id:
                    try:
                        ch = KeysightChassis.objects.get(id=ch_id)
                    except KeysightChassis.DoesNotExist:
                        pass
                ep = None
                try:
                    ep = KeysightBmcEndpoint.objects.get(hostname=bmc_hostname)
                except KeysightBmcEndpoint.DoesNotExist:
                    pass
                user, password = _get_bmc_credentials(ep, ch)
                targets.append({
                    'hostname': bmc_hostname,
                    'ip': bmc_ip,
                    'user': user,
                    'password': password,
                })
                bmc_ips_for_ipmi.append(bmc_hostname)

        if targets:
            from .keysight_aps_generations import aps_gen_from_fru

            infos = fetch_all_bmcs(targets, timeout=6)
            for hostname, info in zip(bmc_ips_for_ipmi, infos):
                fru_prod = info.fru_board_product or ''
                fru_name = info.fru_product_name or ''
                aps_gen = aps_gen_from_fru(fru_prod, fru_name) or ''
                if hostname and (fru_prod or aps_gen):
                    KeysightBmcEndpoint.objects.filter(hostname=hostname).update(
                        fru_board_product=fru_prod,
                        aps_gen=aps_gen if aps_gen in ('10', '15') else '',
                    )
                ipmi_results[hostname.lower()] = {
                    'primary_os_name_raw': info.primary_os_name_raw,
                    'master_host': info.master_host,
                    'mgmt_primary_system': info.mgmt_primary_system,
                    'primary_os_version': info.primary_os_version,
                    'fru_board_mfg': info.fru_board_mfg,
                    'fru_board_product': fru_prod,
                    'fru_board_serial': info.fru_board_serial,
                    'fru_product_name': fru_name,
                    'fru_product_serial': info.fru_product_serial,
                    'aps_gen': aps_gen,
                    'ipmi_reachable': info.reachable,
                    'ipmi_error': info.error,
                }

    # ---- Step 4: Build combined result list ----
    combined = []
    mps_values = set()
    for n in all_nodes:
        bmc_hostname = n.get('bmc_hostname', '')
        ipmi = ipmi_results.get(bmc_hostname.lower(), {})
        ep = bmc_ep_by_hostname.get(bmc_hostname.lower()) if bmc_hostname else None
        relocated_label = ''
        if ep and ep.relocated_from_chassis_id:
            prev = ep.relocated_from_chassis
            relocated_label = (prev.hostname or prev.ip_address) if prev else f'chassis #{ep.relocated_from_chassis_id}'
        entry = {
            'node_name': n.get('node_name', ''),
            'role': n.get('role', ''),
            'operating_mode': n.get('operating_mode', '') or (ep.operating_mode if ep else ''),
            'relocated_from': relocated_label,
            'relocated_at': ep.relocated_at if ep else None,
            'status': n.get('status', ''),
            'internal_ip': n.get('internal_ip', ''),
            'bmc_hostname': bmc_hostname,
            'bmc_ip': n.get('bmc_ip', ''),
            'bmc_name': n.get('bmc_name', ''),
            'bmc_power': n.get('bmc_power', ''),
            'serial_number': n.get('serial_number', ''),
            'bios_version': n.get('bios_version', ''),
            'bmc_firmware': n.get('bmc_firmware', ''),
            'bmc_firmware_name': n.get('bmc_firmware_name', ''),
            'chassis_id': n.get('chassis_id'),
            'chassis_hostname': n.get('chassis_hostname', ''),
            'chassis_type': n.get('chassis_type', ''),
            'source': n.get('source', 'chassis_derived'),
            # IPMI enrichment
            'primary_os_name_raw': ipmi.get('primary_os_name_raw', ''),
            'master_host': ipmi.get('master_host', ''),
            'mgmt_primary_system': ipmi.get('mgmt_primary_system', ''),
            'primary_os_version': ipmi.get('primary_os_version', ''),
            'fru_board_mfg': ipmi.get('fru_board_mfg', ''),
            'fru_board_product': ipmi.get('fru_board_product', ''),
            'fru_board_serial': ipmi.get('fru_board_serial', ''),
            'fru_product_name': ipmi.get('fru_product_name', ''),
            'fru_product_serial': ipmi.get('fru_product_serial', ''),
            'aps_gen': ipmi.get('aps_gen', '') or (ep.aps_gen if ep else '') or n.get('aps_gen', ''),
            'ipmi_reachable': ipmi.get('ipmi_reachable', None),
            'ipmi_error': ipmi.get('ipmi_error', ''),
        }
        combined.append(entry)
        if entry['mgmt_primary_system']:
            mps_values.add(entry['mgmt_primary_system'])

    # Apply filters
    if q:
        combined = [c for c in combined if q in (
            c['node_name'] + c['bmc_hostname'] + c['bmc_ip'] +
            c['serial_number'] + c['chassis_hostname'] +
            c['master_host'] + c['mgmt_primary_system']
        ).lower()]

    if mps_filter:
        combined = [c for c in combined if c['mgmt_primary_system'] == mps_filter]

    if status_filter == 'reachable':
        combined = [c for c in combined if c['bmc_ip']]
    elif status_filter == 'unreachable':
        combined = [c for c in combined if not c['bmc_ip']]

    # Stats
    total = len(combined)
    with_bmc_ip = sum(1 for c in combined if c['bmc_ip'])
    with_mgmt = sum(1 for c in combined if c['mgmt_primary_system'])
    powered_on = sum(1 for c in combined if c['bmc_power'] == 'on')
    total_mgmt_nodes = sum(1 for c in combined if c['role'] == 'Management')
    total_compute = sum(1 for c in combined if c['role'] == 'Compute')

    context = {
        'combined': combined,
        'total': total,
        'with_bmc_ip': with_bmc_ip,
        'without_bmc_ip': total - with_bmc_ip,
        'with_mgmt': with_mgmt,
        'powered_on': powered_on,
        'total_mgmt_nodes': total_mgmt_nodes,
        'total_compute': total_compute,
        'search_q': request.GET.get('q', ''),
        'mps_filter': mps_filter,
        'status_filter': status_filter,
        'chassis_filter': chassis_filter,
        'ipmi_enrich': ipmi_enrich,
        'mps_values': sorted(mps_values),
        'chassis_errors': chassis_errors,
        'all_chassis': KeysightChassis.objects.filter(
            chassis_type__in=list(KCOS_TYPES)),
    }

    # CSV export
    if request.GET.get('format') == 'csv':
        import csv
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="bmc_board.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Node Name', 'Role', 'Status', 'BMC Hostname', 'BMC IP', 'Power',
            'Serial', 'BMC Firmware', 'BIOS Version',
            'primary_os_name (raw)', 'Master Host (mh)', 'Mgmt System (mps)',
            'FRU Product', 'FRU Serial',
            'Chassis', 'Internal IP',
        ])
        for c in combined:
            writer.writerow([
                c['node_name'], c['role'], c['status'],
                c['bmc_hostname'], c['bmc_ip'], c['bmc_power'],
                c['serial_number'], c['bmc_firmware'], c['bios_version'],
                c['primary_os_name_raw'],
                c['master_host'], c['mgmt_primary_system'],
                c['fru_product_name'], c['fru_product_serial'] or c['fru_board_serial'],
                c['chassis_hostname'], c['internal_ip'],
            ])
        return response

    return render(request, 'connect/keysight/bmc_board.html', context)


@login_required
@require_POST
def keysight_bmc_import(request):
    """Import BMC endpoints from hostname:ip lines (hosts.sh format)."""
    raw_text = request.POST.get('bmc_lines', '').strip()
    if not raw_text:
        messages.warning(request, 'No BMC data provided.')
        return redirect('keysight_bmc_board')

    created = 0
    updated = 0
    errors = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # Accept "hostname:ip" or "hostname" (ip blank)
        if ':' in line:
            hostname, _, ip = line.partition(':')
            hostname = hostname.strip()
            ip = ip.strip()
        else:
            hostname = line
            ip = ''

        if not hostname:
            errors.append(f'Empty hostname in line: {line}')
            continue

        try:
            ep, was_created = KeysightBmcEndpoint.objects.update_or_create(
                hostname=hostname,
                defaults={
                    'ip_address': ip,
                    'source': 'manual_import',
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1
        except Exception as exc:
            errors.append(f'{hostname}: {exc}')

    msg = f'Imported: {created} new, {updated} updated.'
    if errors:
        msg += f' {len(errors)} errors: {"; ".join(errors[:5])}'
    messages.success(request, msg)
    return redirect('keysight_bmc_board')


@login_required
@require_POST
def keysight_bmc_delete(request):
    """Delete selected BMC endpoints."""
    ids = request.POST.getlist('bmc_ids')
    if ids:
        deleted, _ = KeysightBmcEndpoint.objects.filter(id__in=ids).delete()
        messages.success(request, f'Deleted {deleted} BMC endpoint(s).')
    return redirect('keysight_bmc_board')


