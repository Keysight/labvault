"""Fleet heartbeat store + probe helpers for Google demo Oncaller APIs.

Seeded mode advances synthetic heartbeats without contacting hardware.
Live mode probes ICMP/TCP livelihood then Keysight drivers for health.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any

from django.utils import timezone

from connect.cache_utils import cache_get, cache_set

logger = logging.getLogger(__name__)

HEARTBEAT_CACHE_KEY = 'fleet_heartbeat:v1'
HEARTBEAT_CACHE_TTL = 86400


def heartbeat_mode() -> str:
    try:
        from connect.runtime_settings import get_setting
        mode = str(get_setting('heartbeat_mode') or 'idle').strip().lower()
    except Exception:
        mode = (os.environ.get('LABVAULT_HEARTBEAT_MODE') or 'idle').strip().lower()
    return mode if mode in ('idle', 'live') else 'idle'


def heartbeat_interval_seconds() -> int:
    """Seconds between live ticks.

    Default is 120s: a full fleet probe of ~100 chassis often takes 1–3 minutes.
    The old 5s default stacked overlapping ticks and looked like mass
    ``missed_heartbeat`` when the worker was merely still probing.
    """
    try:
        return max(15, int(os.environ.get('LABVAULT_HEARTBEAT_INTERVAL_SECONDS', '120')))
    except (TypeError, ValueError):
        return 120


def seeded_mode() -> bool:
    """Return True only when explicitly configured for theater/synthetic heartbeats.

    Default is live real telemetry (empty/unset/live → not seeded).
    """
    raw = (os.environ.get('LABVAULT_HEARTBEAT_MODE') or 'live').strip().lower()
    return raw in ('seeded', 'synthetic', 'demo')


def heartbeat_workers() -> int:
    try:
        return max(1, min(32, int(os.environ.get('LABVAULT_HEARTBEAT_MAX_WORKERS', '12'))))
    except (TypeError, ValueError):
        return 12


def _empty_store() -> dict[str, Any]:
    return {
        'updated_at': timezone.now().isoformat(),
        'interval_seconds': heartbeat_interval_seconds(),
        'mode': 'seeded' if seeded_mode() else heartbeat_mode(),
        'chassis': {},
    }


def load_store() -> dict[str, Any]:
    store = cache_get(HEARTBEAT_CACHE_KEY)
    if isinstance(store, dict) and isinstance(store.get('chassis'), dict):
        return store
    return _empty_store()


def save_store(store: dict[str, Any]) -> None:
    store['updated_at'] = timezone.now().isoformat()
    store['interval_seconds'] = heartbeat_interval_seconds()
    store['mode'] = 'seeded' if seeded_mode() else heartbeat_mode()
    cache_set(HEARTBEAT_CACHE_KEY, store, HEARTBEAT_CACHE_TTL)


def _classify(entry: dict[str, Any], interval: int, now) -> dict[str, Any]:
    last_raw = entry.get('last_heartbeat_at')
    age = None
    if last_raw:
        from django.utils.dateparse import parse_datetime
        last = parse_datetime(last_raw) if isinstance(last_raw, str) else last_raw
        if last is not None:
            if timezone.is_naive(last):
                last = timezone.make_aware(last)
            age = max(0.0, (now - last).total_seconds())
    probe_ok = bool(entry.get('last_probe_ok', False))
    # Live ticks can take > interval on large fleets; allow a wider freshness window.
    max_age = max(3 * interval, 180)
    heartbeat_ok = bool(probe_ok and age is not None and age <= max_age)
    halt_suspect = False
    halt_reason = ''
    if age is None:
        halt_suspect = True
        halt_reason = 'no_heartbeat'
    elif not probe_ok:
        halt_suspect = True
        halt_reason = entry.get('halt_reason') or 'probe_failed'
    elif age > max(6 * interval, 360):
        halt_suspect = True
        halt_reason = 'missed_heartbeat'
    out = dict(entry)
    out['heartbeat_age_s'] = round(age, 2) if age is not None else None
    out['heartbeat_ok'] = heartbeat_ok
    out['halt_suspect'] = halt_suspect
    out['halt_reason'] = halt_reason if halt_suspect else ''
    return out


def fleet_heartbeat_payload() -> dict[str, Any]:
    store = load_store()
    interval = int(store.get('interval_seconds') or heartbeat_interval_seconds())
    now = timezone.now()
    chassis_out = []
    for cid, entry in sorted(store.get('chassis', {}).items(), key=lambda x: str(x[0])):
        if not isinstance(entry, dict):
            continue
        row = _classify(entry, interval, now)
        row['chassis_id'] = int(cid) if str(cid).isdigit() else cid
        chassis_out.append(row)
    return {
        'ok': True,
        'source': 'labvault_fleet_heartbeat',
        'mode': 'seeded' if seeded_mode() else heartbeat_mode(),
        'interval_seconds': interval,
        'updated_at': store.get('updated_at'),
        'generated_at': now.isoformat(),
        'chassis': chassis_out,
        'counts': {
            'total': len(chassis_out),
            'heartbeat_ok': sum(1 for c in chassis_out if c.get('heartbeat_ok')),
            'halt_suspect': sum(1 for c in chassis_out if c.get('halt_suspect')),
            'icmp_ok': sum(1 for c in chassis_out if c.get('icmp')),
            'api_ok': sum(1 for c in chassis_out if c.get('api_probe') == 'ok'),
        },
    }


def upsert_chassis_heartbeat(chassis_id: int, **fields) -> dict[str, Any]:
    store = load_store()
    key = str(chassis_id)
    entry = dict(store.get('chassis', {}).get(key) or {})
    entry.update(fields)
    if 'last_heartbeat_at' not in fields:
        entry['last_heartbeat_at'] = timezone.now().isoformat()
    store.setdefault('chassis', {})[key] = entry
    save_store(store)
    return entry


def _refresh_ports_cache(chassis, driver) -> None:
    """Best-effort port inventory refresh for fleet telemetry/ownership APIs.

    Writes the same cache key as keysight_views._set_cached without importing
    that module (keeps the heartbeat worker import-light).
    """
    try:
        if not hasattr(driver, 'get_ports'):
            return
        result = driver.get_ports()
        if not result or not getattr(result, 'success', False):
            return
        ports = result.data if isinstance(result.data, list) else []
        key = f'keysight:chassis_data:{chassis.pk}'
        cached = cache_get(key) or {}
        if not isinstance(cached, dict):
            cached = {}
        cached['ports'] = ports
        cached['total_ports'] = len(ports)
        cached['ports_up'] = sum(1 for p in ports if p.get('link_state') == 'up')
        cached['ports_free'] = sum(1 for p in ports if p.get('owner', 'Free') == 'Free')
        cached['hostname'] = chassis.hostname or cached.get('hostname') or ''
        cached['chassis_type'] = chassis.chassis_type
        cached['status'] = 'online'
        cached['_cached_at'] = time.time()
        cache_set(key, cached, 600)
    except Exception as exc:
        logger.debug('port cache refresh failed chassis=%s: %s', getattr(chassis, 'pk', '?'), exc)


def _port_refresh_budget() -> int:
    """How many API-ok chassis get a get_ports() refresh each tick (round-robin)."""
    try:
        return max(0, min(32, int(os.environ.get('LABVAULT_HEARTBEAT_PORT_REFRESH_PER_TICK', '8'))))
    except (TypeError, ValueError):
        return 8


def probe_chassis_live(chassis) -> dict[str, Any]:
    """Probe a KeysightChassis for fleet heartbeat.

    ICMP + TCP livelihood first; driver probe adds API/SSH health when available.
    Host reachable with failed API still counts as heartbeat_ok (auth != down).
    """
    from connect.keysight_drivers import get_driver
    from connect.reachability import expand_probe_hosts, probe_hosts

    t0 = time.monotonic()
    entry: dict[str, Any] = {
        'hostname': chassis.hostname or chassis.ip_address,
        'ip_address': chassis.ip_address,
        'chassis_type': chassis.chassis_type,
        'status': chassis.status,
        'team_tags': getattr(chassis, 'team_tags', '') or '',
        'hardware_error_reported': bool(getattr(chassis, 'hardware_error_reported', False)),
        'cpu_pct': None,
        'mem_pct': None,
        'last_probe_ok': False,
        'probe_latency_ms': None,
        'halt_reason': '',
        'icmp': False,
        'open_ports': [],
        'api_probe': '',
        'reachability_host': '',
    }

    reach = probe_hosts(
        expand_probe_hosts(getattr(chassis, 'hostname', '') or '', chassis.ip_address or ''),
        (22, 443, 80),
        try_icmp=True,
        timeout_s=2.0,
    )
    entry['icmp'] = bool(reach.get('icmp'))
    entry['open_ports'] = list(reach.get('open_ports') or [])
    entry['reachability_host'] = reach.get('host') or ''

    try:
        driver = get_driver(chassis)
        probe = driver.probe() if hasattr(driver, 'probe') else 'ok'
        latency = round((time.monotonic() - t0) * 1000, 1)
        entry['probe_latency_ms'] = latency
        entry['api_probe'] = probe

        if probe == 'ok':
            health = driver.get_health() if hasattr(driver, 'get_health') else None
            if health and getattr(health, 'success', False) and isinstance(health.data, dict):
                h = health.data
                entry['cpu_pct'] = h.get('cpu_utilization')
                mem_used = float(h.get('memory_used') or 0)
                mem_total = float(h.get('memory_total') or 0)
                if mem_total > 0:
                    entry['mem_pct'] = round(mem_used / mem_total * 100.0, 2)
            entry['last_probe_ok'] = True
            entry['status'] = 'online'
            # Port refresh is done round-robin in tick_live (not inline per probe).
            return entry

        # API/SSH failed — still heartbeat-ok when ping/TCP says the host is up
        if reach.get('alive'):
            entry['last_probe_ok'] = True
            entry['status'] = 'online'
            entry['halt_reason'] = ''
            return entry

        entry['halt_reason'] = f'probe_{probe}'
        return entry
    except Exception as exc:
        entry['probe_latency_ms'] = round((time.monotonic() - t0) * 1000, 1)
        if reach.get('alive'):
            entry['last_probe_ok'] = True
            entry['status'] = 'online'
            entry['api_probe'] = f'exception:{type(exc).__name__}'
            return entry
        entry['halt_reason'] = f'exception:{type(exc).__name__}'
        logger.debug('heartbeat probe failed chassis=%s: %s', chassis.pk, exc)
    return entry


def tick_seeded(chassis_qs) -> int:
    """Advance synthetic heartbeats for all chassis in queryset."""
    import random

    n = 0
    now = timezone.now()
    for ch in chassis_qs:
        force_fail = (ch.pk % 17 == 0) and (int(now.timestamp()) // 60) % 7 == 0
        entry = {
            'hostname': ch.hostname or ch.ip_address,
            'ip_address': ch.ip_address,
            'chassis_type': ch.chassis_type,
            'status': 'offline' if force_fail else (ch.status or 'online'),
            'team_tags': getattr(ch, 'team_tags', '') or '',
            'hardware_error_reported': bool(getattr(ch, 'hardware_error_reported', False)),
            'cpu_pct': None if force_fail else round(random.uniform(8, 55), 2),
            'mem_pct': None if force_fail else round(random.uniform(20, 70), 2),
            'last_probe_ok': not force_fail,
            'probe_latency_ms': None if force_fail else round(random.uniform(12, 180), 1),
            'halt_reason': 'simulated_api_stall' if force_fail else '',
            'last_heartbeat_at': (now - timedelta(seconds=40 if force_fail else 0)).isoformat(),
            'source': 'seeded',
            'icmp': False,
            'open_ports': [],
            'api_probe': 'seeded',
        }
        upsert_chassis_heartbeat(ch.pk, **entry)
        n += 1
    return n


def tick_live(chassis_qs) -> int:
    """Probe chassis concurrently; write the store once to avoid lost updates."""
    chassis_list = list(chassis_qs)
    if not chassis_list:
        return 0

    def _one(ch):
        fields = probe_chassis_live(ch)
        fields['source'] = 'live'
        return ch.pk, fields

    results: dict[str, dict[str, Any]] = {}
    workers = min(heartbeat_workers(), max(1, len(chassis_list)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, ch) for ch in chassis_list]
        for fut in as_completed(futures):
            try:
                cid, fields = fut.result()
                results[str(cid)] = fields
            except Exception as exc:
                logger.warning('live heartbeat worker failed: %s', exc)

    # Same timestamp for every row so classify age is not skewed by probe order.
    stamped = timezone.now().isoformat()
    for fields in results.values():
        fields['last_heartbeat_at'] = stamped

    store = load_store()
    store['chassis'] = results
    save_store(store)

    # Round-robin port cache refresh for a small budget of API-ok chassis.
    budget = _port_refresh_budget()
    if budget > 0:
        api_ok_ids = [
            int(cid) for cid, fields in results.items()
            if fields.get('api_probe') == 'ok'
        ]
        api_ok_ids.sort()
        if api_ok_ids:
            cursor = int(cache_get('fleet_heartbeat:port_refresh_cursor') or 0)
            selected = []
            for i in range(min(budget, len(api_ok_ids))):
                selected.append(api_ok_ids[(cursor + i) % len(api_ok_ids)])
            cache_set(
                'fleet_heartbeat:port_refresh_cursor',
                (cursor + len(selected)) % max(1, len(api_ok_ids)),
                HEARTBEAT_CACHE_TTL,
            )
            by_id = {ch.pk: ch for ch in chassis_list}
            from connect.keysight_drivers import get_driver
            for cid in selected:
                ch = by_id.get(cid)
                if not ch:
                    continue
                try:
                    _refresh_ports_cache(ch, get_driver(ch))
                except Exception as exc:
                    logger.debug('port refresh skipped chassis=%s: %s', cid, exc)

    return len(results)


def _heartbeat_lock_path() -> str:
    """Prefer a labvault-writable lock path.

    Default used to be ``/tmp/labvault_fleet_heartbeat.lock``. Root
    ``docker compose exec`` recreates that file as root:root mode 0644, then
    the setpriv'd worker (uid labvault) fails every tick with EACCES and the
    store goes stale — every chassis shows ``missed_heartbeat``.
    """
    configured = (os.environ.get('LABVAULT_HEARTBEAT_LOCK') or '').strip()
    candidates = []
    if configured:
        candidates.append(configured)
    # Shared compose cache volume is always chowned to labvault by entrypoint.
    candidates.append('/app/var/django_cache/fleet_heartbeat.lock')
    candidates.append('/var/lib/labvault/fleet_heartbeat.lock')
    candidates.append('/tmp/labvault_fleet_heartbeat.lock')
    for path in candidates:
        parent = os.path.dirname(path) or '.'
        try:
            os.makedirs(parent, exist_ok=True)
            # Probe creatability without leaving a stale exclusive lock.
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
            os.close(fd)
            return path
        except OSError:
            continue
    return candidates[-1]


def run_heartbeat_tick() -> dict[str, Any]:
    """Run one heartbeat tick. Concurrent callers are serialized via a file lock."""
    if heartbeat_mode() == 'idle':
        return {'mode': 'idle', 'counts': {'total': 0, 'heartbeat_ok': 0, 'halt_suspect': 0}}
    import fcntl
    from connect.models import KeysightChassis

    lock_path = _heartbeat_lock_path()
    qs = KeysightChassis.objects.all().order_by('pk')
    with open(lock_path, 'w') as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            if seeded_mode():
                count = tick_seeded(qs)
            else:
                count = tick_live(qs)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
    payload = fleet_heartbeat_payload()
    payload['tick_count'] = count
    return payload
