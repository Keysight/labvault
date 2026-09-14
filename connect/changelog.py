"""Unified change-log writers: status transitions, topology/location moves, offline thresholds."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from .models import (
    ChangeLogEvent,
    Device,
    DeviceStateBaseline,
    KeysightBmcEndpoint,
    KeysightChassis,
)

logger = logging.getLogger(__name__)

UP_STATUSES = frozenset({'online'})
DOWN_STATUSES = frozenset({'offline', 'auth_failed'})
BMC_UP_STATUSES = frozenset({'online'})
BMC_DOWN_STATUSES = frozenset({'offline', 'unknown'})

TEMPORARILY_DOWN_AFTER = timedelta(hours=4)
EXTENDED_DOWN_AFTER = timedelta(hours=24)

def log_change_event(
    event_type: str,
    *,
    target_kind: str,
    target_id: int | None,
    target_repr: str = '',
    detail: str = '',
    old_value: str = '',
    new_value: str = '',
    source: str = 'system',
    actor_username: str = '',
    ip_address: str | None = None,
    user_agent: str = '',
    extra_json: dict | None = None,
) -> ChangeLogEvent | None:
    """Best-effort insert; never raises to callers."""
    try:
        return ChangeLogEvent.objects.create(
            event_type=event_type,
            target_kind=target_kind,
            target_id=target_id,
            target_repr=(target_repr or '')[:500],
            detail=detail or '',
            old_value=(old_value or '')[:500],
            new_value=(new_value or '')[:500],
            source=source,
            actor_username=actor_username or '',
            ip_address=ip_address,
            user_agent=(user_agent or '')[:500],
            extra_json=extra_json,
        )
    except Exception:
        logger.exception('Failed to write ChangeLogEvent %s', event_type)
        return None


def _actor_extra(actor: dict | None) -> dict:
    if not actor:
        return {}
    return {
        k: actor.get(k, '')
        for k in ('browser', 'os', 'device')
        if actor.get(k)
    }


def log_deploy_lifecycle(
    event_type: str,
    *,
    chassis_id: int,
    target_repr: str,
    detail: str = '',
    old_value: str = '',
    new_value: str = '',
    actor: dict | None = None,
    extra_json: dict | None = None,
) -> ChangeLogEvent | None:
    """User-driven deploy / upgrade / downgrade / snapshot events on a chassis."""
    merged = dict(extra_json or {})
    merged.update(_actor_extra(actor))
    return log_change_event(
        event_type,
        target_kind='chassis',
        target_id=chassis_id,
        target_repr=target_repr,
        detail=detail,
        old_value=old_value,
        new_value=new_value,
        source='user',
        actor_username=(actor or {}).get('username', ''),
        ip_address=(actor or {}).get('ip_address'),
        user_agent=(actor or {}).get('user_agent', ''),
        extra_json=merged or None,
    )


def log_status_transition(
    target_kind: str,
    target_id: int,
    target_repr: str,
    old_status: str,
    new_status: str,
    *,
    source: str = 'system',
    actor_username: str = '',
    ip_address: str | None = None,
    user_agent: str = '',
) -> None:
    old_status = (old_status or '').strip()
    new_status = (new_status or '').strip()
    if not new_status or old_status == new_status:
        return

    if old_status in UP_STATUSES and new_status in DOWN_STATUSES:
        event_type = 'status_down'
    elif old_status in DOWN_STATUSES and new_status in UP_STATUSES:
        event_type = 'status_up'
    elif new_status in DOWN_STATUSES and old_status not in DOWN_STATUSES:
        event_type = 'status_down'
    elif new_status in UP_STATUSES and old_status not in UP_STATUSES:
        event_type = 'status_up'
    else:
        return

    log_change_event(
        event_type,
        target_kind=target_kind,
        target_id=target_id,
        target_repr=target_repr,
        old_value=old_status,
        new_value=new_status,
        source=source,
        actor_username=actor_username,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def log_bmc_status_transition(
    endpoint_id: int,
    target_repr: str,
    old_status: str,
    new_status: str,
    *,
    detail: str = '',
    chassis_id: int | None = None,
) -> None:
    """Emit BMC-specific up/down events (IPMI reachability)."""
    old_status = (old_status or '').strip()
    new_status = (new_status or '').strip()
    if not new_status or old_status == new_status:
        return

    if old_status in BMC_UP_STATUSES and new_status in BMC_DOWN_STATUSES:
        event_type = 'bmc_down'
    elif old_status in BMC_DOWN_STATUSES and new_status in BMC_UP_STATUSES:
        event_type = 'bmc_up'
    elif new_status in BMC_DOWN_STATUSES and old_status not in BMC_DOWN_STATUSES:
        event_type = 'bmc_down'
    elif new_status in BMC_UP_STATUSES and old_status not in BMC_UP_STATUSES:
        event_type = 'bmc_up'
    else:
        return

    extra = {'chassis_id': chassis_id} if chassis_id else None
    log_change_event(
        event_type,
        target_kind='bmc',
        target_id=endpoint_id,
        target_repr=target_repr,
        detail=(detail or '')[:500],
        old_value=old_status,
        new_value=new_status,
        extra_json=extra,
    )


def _chassis_repr(ch: KeysightChassis) -> str:
    return str(ch.hostname or ch.connect_address or ch.ip_address or ch.pk)


def _device_repr(dev: Device) -> str:
    return str(dev.hostname or dev.connect_address or dev.ip_address or dev.pk)


def _bmc_repr(ep: KeysightBmcEndpoint) -> str:
    node = (ep.node_name or '').strip()
    host = (ep.hostname or '').strip()
    if node and host and node.lower() != host.lower():
        return f'{node} ({host})'
    return host or node or str(ep.pk)


def _bmc_credentials(ep: KeysightBmcEndpoint) -> tuple[str, str]:
    import os

    if ep.username:
        return ep.username, ep.password
    ch = ep.chassis
    if ch:
        return ch.username, ch.password
    return (
        os.environ.get('BMC_DEFAULT_USER', 'admin'),
        os.environ.get('BMC_DEFAULT_PASS', 'admin'),
    )


def _resolve_bmc_ip(hostname: str, domain_suffix: str = '') -> str:
    import socket

    if not hostname:
        return ''
    candidates = [hostname]
    if domain_suffix and '.' not in hostname:
        candidates.append(f'{hostname}.{domain_suffix}')
    for name in candidates:
        try:
            return socket.gethostbyname(name)
        except OSError:
            continue
    return ''


def _snapshot_for_bmc(ep: KeysightBmcEndpoint, *, ipmi_status: str, error: str = '') -> dict:
    ch = ep.chassis
    return {
        'target_repr': _bmc_repr(ep),
        'ip': (ep.ip_address or '')[:64],
        'lab': (ch.lab_name if ch else '')[:200],
        'geo': (ch.geo_location if ch else '')[:200],
        'site': (ch.site if ch else '')[:200],
        'rack': '',
        'peer_summary': (ch.hostname if ch else '')[:500],
        'status': (ipmi_status or 'unknown')[:20],
        'detail': (error or '')[:500],
    }


def _snapshot_for_chassis(ch: KeysightChassis) -> dict:
    return {
        'target_repr': _chassis_repr(ch),
        'ip': (ch.connect_address or ch.ip_address or '')[:64],
        'lab': (ch.lab_name or '')[:200],
        'geo': (ch.geo_location or '')[:200],
        'site': (ch.site or '')[:200],
        'rack': '',
        'peer_summary': '',
        'status': (ch.status or '')[:20],
    }


def _snapshot_for_device(dev: Device) -> dict:
    return {
        'target_repr': _device_repr(dev),
        'ip': (dev.connect_address or dev.ip_address or '')[:64],
        'lab': (dev.group_name or '')[:200],
        'geo': (dev.site or '')[:200],
        'site': (dev.site or '')[:200],
        'rack': (dev.rack or '')[:100],
        'peer_summary': '',
        'status': (dev.status or '')[:20],
    }


def _apply_snapshot_to_baseline(baseline: DeviceStateBaseline, snap: dict) -> None:
    for key in (
        'target_repr', 'ip', 'lab', 'geo', 'site', 'rack', 'peer_summary', 'status',
    ):
        setattr(baseline, key, snap.get(key, '') or '')


def _emit_field_moves(
    baseline: DeviceStateBaseline,
    snap: dict,
    *,
    target_kind: str,
    target_id: int,
) -> int:
    count = 0
    repr_ = snap.get('target_repr', '')

    old_ip = (baseline.ip or '').strip()
    new_ip = (snap.get('ip') or '').strip()
    if old_ip and new_ip and old_ip != new_ip:
        log_change_event(
            'moved_ip',
            target_kind=target_kind,
            target_id=target_id,
            target_repr=repr_,
            detail='IP changed',
            old_value=old_ip,
            new_value=new_ip,
        )
        count += 1

    old_lab = (baseline.lab or '').strip()
    new_lab = (snap.get('lab') or '').strip()
    if old_lab and new_lab and old_lab != new_lab:
        log_change_event(
            'moved_location',
            target_kind=target_kind,
            target_id=target_id,
            target_repr=repr_,
            detail='Lab changed',
            old_value=old_lab,
            new_value=new_lab,
        )
        count += 1

    old_topo = '|'.join(
        x for x in [(baseline.geo or '').strip(), (baseline.site or '').strip(), (baseline.peer_summary or '').strip()] if x
    )
    new_topo = '|'.join(
        x for x in [(snap.get('geo') or '').strip(), (snap.get('site') or '').strip(), (snap.get('peer_summary') or '').strip()] if x
    )
    if old_topo and new_topo and old_topo != new_topo:
        log_change_event(
            'moved_topology',
            target_kind=target_kind,
            target_id=target_id,
            target_repr=repr_,
            detail='Topology / site context changed',
            old_value=old_topo[:500],
            new_value=new_topo[:500],
        )
        count += 1

    return count


def _emit_offline_thresholds(baseline: DeviceStateBaseline, snap: dict, *, target_kind: str, target_id: int) -> int:
    count = 0
    now = timezone.now()
    status = snap.get('status') or ''
    if status in DOWN_STATUSES:
        if not baseline.last_offline_at:
            baseline.last_offline_at = now
        offline_for = now - baseline.last_offline_at
        if offline_for >= EXTENDED_DOWN_AFTER and baseline.last_emitted_state != 'extended_down':
            log_change_event(
                'extended_down',
                target_kind=target_kind,
                target_id=target_id,
                target_repr=snap.get('target_repr', ''),
                detail=f'Offline for {int(offline_for.total_seconds() // 3600)}h',
                old_value=baseline.status,
                new_value=status,
            )
            baseline.last_emitted_state = 'extended_down'
            count += 1
        elif (
            offline_for >= TEMPORARILY_DOWN_AFTER
            and baseline.last_emitted_state not in ('temporarily_down', 'extended_down')
        ):
            log_change_event(
                'temporarily_down',
                target_kind=target_kind,
                target_id=target_id,
                target_repr=snap.get('target_repr', ''),
                detail=f'Offline for {int(offline_for.total_seconds() // 3600)}h',
                old_value=baseline.status,
                new_value=status,
            )
            baseline.last_emitted_state = 'temporarily_down'
            count += 1
    else:
        baseline.last_offline_at = None
        if baseline.last_emitted_state:
            baseline.last_emitted_state = ''
    return count


def compare_snap_to_baseline(
    target_kind: str,
    target_id: int,
    snap: dict,
    *,
    emit_status: bool = True,
) -> int:
    """Compare current snapshot to stored baseline; emit events; update baseline. Returns event count."""
    baseline, created = DeviceStateBaseline.objects.get_or_create(
        target_kind=target_kind,
        target_id=target_id,
        defaults={'target_repr': snap.get('target_repr', '')},
    )
    events = 0
    if emit_status and not created:
        if target_kind == 'bmc':
            log_bmc_status_transition(
                target_id,
                snap.get('target_repr', ''),
                baseline.status,
                snap.get('status', ''),
                detail=snap.get('detail', ''),
                chassis_id=(snap.get('chassis_id') or None),
            )
        else:
            log_status_transition(
                target_kind,
                target_id,
                snap.get('target_repr', ''),
                baseline.status,
                snap.get('status', ''),
            )

    if not created:
        events += _emit_field_moves(baseline, snap, target_kind=target_kind, target_id=target_id)
        events += _emit_offline_thresholds(baseline, snap, target_kind=target_kind, target_id=target_id)

    _apply_snapshot_to_baseline(baseline, snap)
    baseline.last_seen_at = timezone.now()
    baseline.save()
    return events


def scan_chassis(ch: KeysightChassis) -> int:
    return compare_snap_to_baseline('chassis', ch.id, _snapshot_for_chassis(ch))


def scan_device(dev: Device) -> int:
    return compare_snap_to_baseline('device', dev.id, _snapshot_for_device(dev))


def scan_bmc_endpoint(ep: KeysightBmcEndpoint, *, timeout: int = 6) -> int:
    """Probe BMC via IPMI and update baseline / change log."""
    from .bmc_ipmi import probe_bmc_reachable

    import os

    ip = (ep.ip_address or '').strip()
    if not ip and ep.hostname:
        ip = _resolve_bmc_ip(ep.hostname, os.environ.get('BMC_DOMAIN_SUFFIX', ''))
        if ip and ip != ep.ip_address:
            ep.ip_address = ip
            ep.save(update_fields=['ip_address', 'updated_at'])

    if not ip:
        snap = _snapshot_for_bmc(ep, ipmi_status='unknown', error='No BMC IP')
        return compare_snap_to_baseline('bmc', ep.id, snap, emit_status=False)

    user, password = _bmc_credentials(ep)
    reachable, error = probe_bmc_reachable(ip, user, password, timeout=timeout)
    status = 'online' if reachable else 'offline'
    now = timezone.now()
    ep.last_error = '' if reachable else (error or 'BMC unreachable')[:500]
    if reachable:
        ep.last_seen = now
    ep.save(update_fields=['last_error', 'last_seen', 'updated_at', 'ip_address'])

    snap = _snapshot_for_bmc(ep, ipmi_status=status, error=ep.last_error)
    snap['chassis_id'] = ep.chassis_id
    return compare_snap_to_baseline('bmc', ep.id, snap)


def scan_all_bmcs(*, timeout: int = 6, max_workers: int = 10) -> dict:
    """Probe all known BMC endpoints in parallel. Returns counts."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    eps = list(
        KeysightBmcEndpoint.objects.select_related('chassis').order_by('hostname')
    )
    stats = {'bmcs': 0, 'events': 0, 'reachable': 0, 'unreachable': 0}
    if not eps:
        return stats

    def _run(ep: KeysightBmcEndpoint) -> tuple[int, str]:
        ev = scan_bmc_endpoint(ep, timeout=timeout)
        ip = (ep.ip_address or '').strip()
        if ip and not ep.last_error:
            return ev, 'reachable'
        if ip:
            return ev, 'unreachable'
        return ev, 'skipped'

    workers = min(max_workers, len(eps))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run, ep): ep for ep in eps}
        for future in as_completed(futures):
            try:
                ev_count, kind = future.result()
                stats['events'] += ev_count
                stats['bmcs'] += 1
                if kind == 'reachable':
                    stats['reachable'] += 1
                elif kind == 'unreachable':
                    stats['unreachable'] += 1
            except Exception:
                logger.exception('BMC changelog scan failed for %s', futures[future].hostname)
                stats['bmcs'] += 1

    return stats


def scan_all_targets(*, bmc_timeout: int = 6) -> dict:
    """Run baseline scan for devices, chassis, and BMC endpoints."""
    stats = {'devices': 0, 'chassis': 0, 'bmcs': 0, 'events': 0}
    for dev in Device.objects.all().iterator():
        stats['events'] += scan_device(dev)
        stats['devices'] += 1
    for ch in KeysightChassis.objects.all().iterator():
        stats['events'] += scan_chassis(ch)
        stats['chassis'] += 1
    bmc_stats = scan_all_bmcs(timeout=bmc_timeout)
    stats['bmcs'] = bmc_stats['bmcs']
    stats['events'] += bmc_stats['events']
    stats['bmc_reachable'] = bmc_stats.get('reachable', 0)
    stats['bmc_unreachable'] = bmc_stats.get('unreachable', 0)
    return stats
