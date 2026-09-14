# HARD_DUMP_REMOVED — customer stub/compat
"""LabVault Diagnostics Center — comprehensive health and incident bundle."""
from __future__ import annotations

import io
import json
import os
import platform
import socket
import subprocess
import sys
import tarfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import connections
from django.test import Client
from django.utils import timezone as dj_tz

from connect.cache_utils import cache_get, cache_set
from connect.demo_mode import demo_mode_enabled
from connect.fleet_heartbeat import fleet_heartbeat_payload, load_store
from connect.log_ring import recent_log_entries
from connect.diagnostics_log_store import read_host_logs, host_log_bundle_files
from connect.metric_collectors import (
    collector_mode,
    collector_node_types,
    collector_topology_allowlist,
)
from connect.worker_status import worker_status_payload

_SECRET_SUBSTRINGS = (
    'password', 'secret', 'token', 'key', 'credential', 'auth', 'private',
)

_CHECK = dict[str, Any]


def _git_revision(base_dir: Path | None = None) -> str | None:
    root = base_dir or Path(getattr(settings, 'BASE_DIR', '.'))
    try:
        out = subprocess.check_output(
            ['git', '-C', str(root), 'rev-parse', '--short', 'HEAD'],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        return out.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _redact_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in sorted(os.environ.items()):
        if not key.startswith(('LABVAULT_', 'DJANGO_', 'DATABASE_', 'NP_', 'GUNICORN_')):
            continue
        lk = key.lower()
        if any(s in lk for s in _SECRET_SUBSTRINGS):
            out[key] = '***redacted***' if val else ''
        else:
            out[key] = val
    return out


def _add_check(
    checks: list[_CHECK],
    *,
    name: str,
    category: str,
    ok: bool,
    detail: str,
    severity: str = 'fail',
    remediation: str = '',
) -> None:
    if ok:
        severity = 'ok'
    elif severity not in ('warn', 'fail'):
        severity = 'fail'
    checks.append({
        'name': name,
        'category': category,
        'ok': ok,
        'severity': severity,
        'detail': detail,
        'remediation': remediation if not ok else '',
    })


def _db_check(alias: str) -> dict[str, Any]:
    info: dict[str, Any] = {'alias': alias, 'reachable': False}
    try:
        conn = connections[alias]
        conn.ensure_connection()
        info['reachable'] = True
        info['vendor'] = conn.vendor
        info['engine'] = conn.settings_dict.get('ENGINE', '')
        with conn.cursor() as cur:
            if conn.vendor == 'postgresql':
                cur.execute('SHOW max_connections')
                info['max_connections'] = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT count(*)::int, "
                    "count(*) FILTER (WHERE state = 'active')::int, "
                    "count(*) FILTER (WHERE state = 'idle')::int "
                    'FROM pg_stat_activity'
                )
                total, active, idle = cur.fetchone()
                info['connections_total'] = total
                info['connections_active'] = active
                info['connections_idle'] = idle
                cur.execute(
                    "SELECT coalesce(application_name, ''), state, count(*)::int "
                    'FROM pg_stat_activity GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15'
                )
                info['connections_by_app'] = [
                    {'application': r[0] or '(none)', 'state': r[1], 'count': r[2]}
                    for r in cur.fetchall()
                ]
            elif conn.vendor == 'sqlite':
                cur.execute('SELECT 1')
                db_path = Path(str(conn.settings_dict.get('NAME', '')))
                info['sqlite_path'] = str(db_path)
                if db_path.is_file():
                    info['sqlite_size_mb'] = round(db_path.stat().st_size / (1024 * 1024), 2)
    except Exception as exc:
        info['error'] = f'{type(exc).__name__}: {exc}'
    return info


def _disk_mem() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        import shutil

        base = Path(getattr(settings, 'BASE_DIR', '.'))
        usage = shutil.disk_usage(base)
        out['disk_total_gb'] = round(usage.total / (1024 ** 3), 2)
        out['disk_used_gb'] = round(usage.used / (1024 ** 3), 2)
        out['disk_free_gb'] = round(usage.free / (1024 ** 3), 2)
        out['disk_used_pct'] = round(usage.used / usage.total * 100, 1) if usage.total else 0
    except OSError as exc:
        out['disk_error'] = str(exc)
    try:
        import resource

        ru = resource.getrusage(resource.RUSAGE_SELF)
        out['process_max_rss_mb'] = round(ru.ru_maxrss / 1024, 1)
    except Exception:
        pass
    return out


def _path_sizes() -> list[dict[str, Any]]:
    base = Path(getattr(settings, 'BASE_DIR', '.'))
    paths = [
        ('django_cache', os.environ.get('LABVAULT_CACHE_DIR', str(base / 'var' / 'django_cache'))),
        ('np_timeseries', str(settings.DATABASES.get('np_timeseries', {}).get('NAME', ''))),
        ('media', getattr(settings, 'MEDIA_ROOT', base / 'media')),
    ]
    rows = []
    for label, raw in paths:
        p = Path(raw) if raw else None
        row: dict[str, Any] = {'label': label, 'path': str(p) if p else ''}
        if p and p.exists():
            if p.is_file():
                row['size_mb'] = round(p.stat().st_size / (1024 * 1024), 2)
            elif p.is_dir():
                total = sum(f.stat().st_size for f in p.rglob('*') if f.is_file())
                row['size_mb'] = round(total / (1024 * 1024), 2)
            row['exists'] = True
        else:
            row['exists'] = False
        rows.append(row)
    return rows


def _inventory_summary() -> dict[str, Any]:
    from connect.models import Alert, Device, KeysightChassis, LabTopology

    devices = Device.objects.all()
    chassis = KeysightChassis.objects.all()
    now = dj_tz.now()
    return {
        'devices_total': devices.count(),
        'devices_online': devices.filter(status='online').count(),
        'devices_auth_failed': devices.filter(status='auth_failed').count(),
        'devices_offline': devices.filter(status='offline').count(),
        'devices_maintenance': devices.filter(maintenance_mode=True).count(),
        'chassis_total': chassis.count(),
        'chassis_online': chassis.filter(status='online').count(),
        'chassis_auth_failed': chassis.filter(status='auth_failed').count(),
        'chassis_offline': chassis.filter(status='offline').count(),
        'topologies_total': LabTopology.objects.count(),
        'alerts_unacknowledged': Alert.objects.filter(acknowledged=False).count(),
        'alerts_critical_open': Alert.objects.filter(
            acknowledged=False, severity='critical',
        ).count(),
        'devices_auth_failed_list': list(
            devices.filter(status='auth_failed').values('id', 'ip_address', 'hostname', 'vendor_type')[:25]
        ),
        'chassis_auth_failed_list': list(
            chassis.filter(status='auth_failed').values('id', 'ip_address', 'hostname', 'chassis_type')[:25]
        ),
        'chassis_offline_list': list(
            chassis.filter(status='offline').values('id', 'ip_address', 'hostname')[:25]
        ),
    }


def _collector_stats() -> dict[str, Any]:
    from connect.models import LabMetricSample

    out: dict[str, Any] = {
        'mode': collector_mode(),
        'node_types': collector_node_types(),
        'topology_allowlist': collector_topology_allowlist(),
        'switch_discards_seeded': os.environ.get('LABVAULT_COLLECTOR_SWITCH_DISCARDS_SEEDED', ''),
    }
    try:
        qs = LabMetricSample.objects.using('np_timeseries')
        out['sample_count'] = qs.count()
        latest = qs.order_by('-sampled_at').values('sampled_at', 'metric', 'topology_id').first()
        if latest:
            out['latest_sample_at'] = latest['sampled_at'].isoformat()
            age_s = (dj_tz.now() - latest['sampled_at']).total_seconds()
            out['latest_sample_age_s'] = round(age_s, 1)
            out['latest_metric'] = latest['metric']
            out['latest_topology_id'] = latest['topology_id']
        since = dj_tz.now() - timedelta(hours=1)
        out['samples_last_hour'] = qs.filter(sampled_at__gte=since).count()
    except Exception as exc:
        out['error'] = f'{type(exc).__name__}: {exc}'
    return out


def _heartbeat_detail() -> dict[str, Any]:
    hb = fleet_heartbeat_payload()
    store = load_store()
    stale = []
    interval = int(hb.get('interval_seconds') or 5)
    for row in hb.get('chassis') or []:
        if row.get('halt_suspect') or not row.get('heartbeat_ok'):
            stale.append({
                'chassis_id': row.get('chassis_id'),
                'hostname': row.get('hostname'),
                'halt_reason': row.get('halt_reason'),
                'heartbeat_age_s': row.get('heartbeat_age_s'),
            })
    return {
        'payload': {
            'mode': hb.get('mode'),
            'interval_seconds': interval,
            'updated_at': hb.get('updated_at'),
            'counts': hb.get('counts'),
        },
        'store_updated_at': store.get('updated_at'),
        'stale_chassis': stale[:50],
    }


def _api_smoke_tests() -> list[dict[str, Any]]:
    """In-process HTTP smoke tests (no external network)."""
    client = Client()
    paths = [
        ('login_page', '/login/', 200),
        ('fleet_health', '/api/fleet/health.json', 401),
        ('fleet_openapi', '/api/fleet/openapi.json', 200),
        ('diagnostics_api', '/api/diagnostics.json', 401),
    ]
    rows = []
    for name, path, expected in paths:
        t0 = time.monotonic()
        try:
            resp = client.get(path)
            ms = round((time.monotonic() - t0) * 1000, 1)
            ok = resp.status_code == expected
            rows.append({
                'name': name,
                'path': path,
                'ok': ok,
                'status_code': resp.status_code,
                'expected': expected,
                'latency_ms': ms,
            })
        except Exception as exc:
            rows.append({
                'name': name,
                'path': path,
                'ok': False,
                'error': f'{type(exc).__name__}: {exc}',
            })
    return rows


def _django_system_check() -> dict[str, Any]:
    from django.core.management import call_command

    buf = io.StringIO()
    try:
        call_command('check', stdout=buf, stderr=buf)
        text = buf.getvalue().strip()
        return {'ok': True, 'detail': text or 'System check identified no issues.'}
    except Exception as exc:
        return {'ok': False, 'detail': buf.getvalue().strip() or f'{type(exc).__name__}: {exc}'}


def _topology_insights_check() -> dict[str, Any]:
    from django.core.management import call_command

    buf = io.StringIO()
    try:
        call_command('ensure_topology_insights', '--check-only', stdout=buf, stderr=buf)
        return {'ok': True, 'detail': buf.getvalue().strip() or 'ready'}
    except Exception as exc:
        return {'ok': False, 'detail': buf.getvalue().strip() or f'{type(exc).__name__}: {exc}'}


def _ocs_status() -> dict[str, Any]:
    from connect.drivers import get_driver
    from connect.models import Device

    ocs = Device.objects.filter(vendor_type__icontains='ocs').first()
    if ocs is None:
        return {'configured': False}
    row = {
        'configured': True,
        'device_id': ocs.pk,
        'ip_address': ocs.ip_address,
        'hostname': ocs.hostname,
        'status': ocs.status,
        'username': ocs.username,
    }
    try:
        drv = get_driver(ocs)
        probe = drv.probe() if hasattr(drv, 'probe') else 'unknown'
        row['probe'] = probe
        if probe == 'ok' and hasattr(drv, 'fetch_crossconnect_list'):
            xcons = drv.fetch_crossconnect_list() or []
            row['crossconnect_count'] = len(xcons)
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {exc}'
    return row


def _migration_health_check() -> dict[str, Any]:
    from django.core.management import call_command

    buf = io.StringIO()
    try:
        call_command('labvault_migration_health', stdout=buf, stderr=buf)
        return {'ok': True, 'detail': buf.getvalue().strip() or 'ok'}
    except SystemExit as exc:
        code = int(getattr(exc, 'code', 1) or 1)
        return {'ok': code == 0, 'detail': buf.getvalue().strip() or f'exit {code}'}
    except Exception as exc:
        return {'ok': False, 'detail': buf.getvalue().strip() or f'{type(exc).__name__}: {exc}'}


def _driver_probe_summary(*, limit: int = 5) -> dict[str, Any]:
    from connect.drivers import get_driver
    from connect.models import Device, KeysightChassis

    rows: list[dict[str, Any]] = []
    for dev in Device.objects.filter(status__in=('auth_failed', 'offline')).order_by('ip_address')[:limit]:
        row = {'kind': 'device', 'id': dev.pk, 'ip': dev.ip_address, 'vendor': dev.vendor_type, 'status': dev.status}
        try:
            drv = get_driver(dev)
            row['probe'] = drv.probe() if hasattr(drv, 'probe') else 'unknown'
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
        rows.append(row)
    for ch in KeysightChassis.objects.filter(status__in=('auth_failed', 'offline')).order_by('ip_address')[:limit]:
        rows.append({
            'kind': 'chassis', 'id': ch.pk, 'ip': ch.ip_address,
            'type': ch.chassis_type, 'status': ch.status,
        })
    return {'samples': rows, 'auth_failed_devices': Device.objects.filter(status='auth_failed').count()}


def _process_health() -> dict[str, Any]:
    out: dict[str, Any] = {'compose_hint': os.environ.get('COMPOSE_PROJECT_NAME', '')}
    try:
        proc = subprocess.run(
            ['docker', 'compose', 'ps', '--format', 'json'],
            capture_output=True, text=True, timeout=8,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
            parsed = []
            for ln in lines:
                try:
                    parsed.append(json.loads(ln))
                except json.JSONDecodeError:
                    parsed.append({'raw': ln})
            out['compose_ps'] = parsed
    except (OSError, subprocess.SubprocessError) as exc:
        out['compose_error'] = str(exc)
    return out


def _run_all_checks(
    db_default: dict[str, Any],
    db_ts: dict[str, Any],
    inventory: dict[str, Any],
    collector: dict[str, Any],
    heartbeat: dict[str, Any],
    django_check: dict[str, Any],
    topo_check: dict[str, Any],
    api_smoke: list[dict[str, Any]],
    resources: dict[str, Any],
    ocs: dict[str, Any],
    migration: dict[str, Any],
    workers: dict[str, Any],
    host_logs: dict[str, Any],
) -> list[_CHECK]:
    checks: list[_CHECK] = []

    _add_check(checks, name='django_system_check', category='platform',
               ok=django_check.get('ok'), detail=django_check.get('detail', ''))

    disk_pct = resources.get('disk_used_pct', 0)
    _add_check(checks, name='disk_headroom', category='platform',
               ok=disk_pct < 90, severity='warn' if disk_pct < 95 else 'fail',
               detail=f"{resources.get('disk_free_gb', '?')} GB free ({disk_pct}% used)",
               remediation='Free disk space or expand volume before DB/cache fills.')

    _add_check(checks, name='database_default', category='database',
               ok=db_default.get('reachable'), detail=db_default.get('error') or 'ok',
               remediation='Restart postgres; verify DATABASE_URL; check connection leaks.')

    _add_check(checks, name='database_np_timeseries', category='database',
               ok=db_ts.get('reachable'), detail=db_ts.get('error') or 'ok',
               remediation='Run ensure_topology_insights; verify NP_TIMESERIES_DATABASE_URL path.')

    max_conn = db_default.get('max_connections')
    total_conn = db_default.get('connections_total')
    if max_conn and total_conn is not None:
        pct = round(total_conn / max_conn * 100, 1)
        _add_check(checks, name='postgres_connection_headroom', category='database',
                   ok=total_conn < max_conn * 0.85,
                   severity='warn' if total_conn < max_conn * 0.95 else 'fail',
                   detail=f'{total_conn}/{max_conn} connections ({pct}%)',
                   remediation='Restart postgres to drop leaks; verify close_old_connections in refresh threads.')

    _add_check(checks, name='cache_read_write', category='services', ok=_cache_ping(),
               detail='file cache set/get', remediation='Check LABVAULT_CACHE_DIR permissions.')

    _add_check(checks, name='topology_insights', category='services',
               ok=topo_check.get('ok'), detail=topo_check.get('detail', ''))

    _add_check(checks, name='migration_health', category='platform',
               ok=migration.get('ok'), severity='warn',
               detail=migration.get('detail', '')[:500],
               remediation='Run python manage.py migrate after backup.')

    if workers.get('device_refresh_thread', {}).get('stale'):
        _add_check(checks, name='device_refresh_thread', category='workers',
                   ok=False, severity='warn',
                   detail=json.dumps(workers.get('device_refresh_thread'), default=str),
                   remediation='Check gunicorn workers and device refresh thread logs.')
    if workers.get('keysight_refresh_thread', {}).get('stale'):
        _add_check(checks, name='keysight_refresh_thread', category='workers',
                   ok=False, severity='warn',
                   detail=json.dumps(workers.get('keysight_refresh_thread'), default=str),
                   remediation='Verify KS refresh leader lock and chassis API reachability.')

    if host_logs.get('configured') and not host_logs.get('entries'):
        _add_check(checks, name='aggregated_logs', category='logs',
                   ok=False, severity='warn',
                   detail='log agent configured but no entries yet',
                   remediation='Populate LABVAULT_DIAGNOSTICS_LOG_DIR with JSONL logs or use diagnostics ingest API (customer SKU has no docker log-agent).')

    hb_counts = heartbeat.get('payload', {}).get('counts') or {}
    hb_ok = hb_counts.get('halt_suspect', 0) == 0 and hb_counts.get('total', 0) > 0
    _add_check(checks, name='fleet_heartbeat', category='services',
               ok=hb_ok, severity='warn',
               detail=(
                   f"mode={heartbeat.get('payload', {}).get('mode')} "
                   f"ok={hb_counts.get('heartbeat_ok')}/{hb_counts.get('total')} "
                   f"halt={hb_counts.get('halt_suspect')}"
               ),
               remediation='Restart heartbeat service; check fleet_heartbeat cache and DB.')

    age = collector.get('latest_sample_age_s')
    if collector.get('error'):
        _add_check(checks, name='metrics_collector', category='services', ok=False,
                   detail=collector['error'],
                   remediation='Run ensure_topology_insights; restart collector service.')
    elif age is None:
        _add_check(checks, name='metrics_collector', category='services', ok=False,
                   detail='no samples in np_timeseries',
                   remediation='Start labvault-collector / compose collector service.')
    else:
        max_age = 900 if collector.get('mode') == 'live' else 3600
        _add_check(checks, name='metrics_collector', category='services',
                   ok=age < max_age, severity='warn' if age < max_age * 2 else 'fail',
                   detail=f"mode={collector.get('mode')} latest_sample_age_s={age} samples_1h={collector.get('samples_last_hour')}",
                   remediation='Restart collector; verify LABVAULT_COLLECTOR_* env and topology IDs.')

    if ocs.get('configured'):
        _add_check(checks, name='ocs_controller', category='integrations',
                   ok=ocs.get('probe') == 'ok', detail=json.dumps({
                       'ip': ocs.get('ip_address'), 'probe': ocs.get('probe'),
                       'xcons': ocs.get('crossconnect_count'),
                   }),
                   remediation='Fix OCS Device credentials; verify REST reachability.')

    if inventory.get('devices_auth_failed'):
        _add_check(checks, name='devices_auth_failed', category='inventory',
                   ok=False, severity='warn',
                   detail=f"{inventory['devices_auth_failed']} device(s) auth_failed",
                   remediation='Update device credentials on Device records.')

    if inventory.get('chassis_auth_failed'):
        _add_check(checks, name='chassis_auth_failed', category='inventory',
                   ok=False, severity='warn',
                   detail=f"{inventory['chassis_auth_failed']} chassis auth_failed",
                   remediation='Verify KeysightChassis API credentials.')

    for row in api_smoke:
        _add_check(checks, name=f"http_{row['name']}", category='api',
                   ok=row.get('ok'), detail=json.dumps(row, default=str))

    return checks


def _cache_ping() -> bool:
    token = f'lv-diag-{int(time.time())}'
    try:
        cache_set(token, 'pong', 30)
        return cache_get(token) == 'pong'
    except Exception:
        return False


def _summary(checks: list[_CHECK]) -> dict[str, Any]:
    passed = sum(1 for c in checks if c.get('ok'))
    failed = sum(1 for c in checks if not c.get('ok') and c.get('severity') == 'fail')
    warnings = sum(1 for c in checks if not c.get('ok') and c.get('severity') == 'warn')
    return {
        'total': len(checks),
        'passed': passed,
        'failed': failed,
        'warnings': warnings,
        'overall_ok': failed == 0,
    }


def _recommendations(checks: list[_CHECK]) -> list[str]:
    recs = []
    for c in checks:
        if not c.get('ok') and c.get('remediation'):
            recs.append(f"{c['name']}: {c['remediation']}")
    return recs


def build_diagnostics_payload(*, include_logs: bool = True, log_limit: int = 500) -> dict[str, Any]:
    """Full structured diagnostics snapshot."""
    now = dj_tz.now()
    db_default = _db_check('default')
    db_ts = _db_check('np_timeseries')
    inventory = _inventory_summary()
    collector = _collector_stats()
    heartbeat = _heartbeat_detail()
    django_check = _django_system_check()
    topo_check = _topology_insights_check()
    api_smoke = _api_smoke_tests()
    resources = _disk_mem()
    ocs = _ocs_status()
    path_sizes = _path_sizes()
    migration = _migration_health_check()
    workers = worker_status_payload()
    host_logs = read_host_logs(limit=log_limit)
    driver_probes = _driver_probe_summary()
    processes = _process_health()
    checks = _run_all_checks(
        db_default, db_ts, inventory, collector, heartbeat,
        django_check, topo_check, api_smoke, resources, ocs,
        migration, workers, host_logs,
    )
    summary = _summary(checks)

    payload: dict[str, Any] = {
        'ok': summary['overall_ok'],
        'generated_at': now.isoformat(),
        'hostname': socket.gethostname(),
        'platform': platform.platform(),
        'python': sys.version.split()[0],
        'summary': summary,
        'recommendations': _recommendations(checks),
        'checks': checks,
        'sections': {
            'platform': {
                'labvault': {
                    'revision': _git_revision(),
                    'demo_mode': demo_mode_enabled(),
                    'debug': bool(getattr(settings, 'DEBUG', False)),
                },
                'django_check': django_check,
                'resources': resources,
                'path_sizes': path_sizes,
                'driver_registry': __import__('connect.driver_registry', fromlist=['registry_summary']).registry_summary(),
            },
            'databases': {
                'default': db_default,
                'np_timeseries': db_ts,
            },
            'services': {
                'collector': collector,
                'heartbeat': heartbeat,
                'cache_ok': _cache_ping(),
            },
            'integrations': {
                'ocs': ocs,
            },
            'inventory': inventory,
            'api_smoke': api_smoke,
            'topology_insights': topo_check,
            'host_logs': host_logs,
            'workers': workers,
            'processes': processes,
            'driver_probes': driver_probes,
            'migration_health': migration,
        },
        'environment': _redact_env(),
        # Legacy flat keys for short export / backwards compatibility
        'database': {'default': db_default, 'np_timeseries': db_ts},
        'fleet_heartbeat': heartbeat.get('payload'),
        'inventory_summary': {k: v for k, v in inventory.items() if not k.endswith('_list')},
        'resources': resources,
        'labvault': {
            'revision': _git_revision(),
            'demo_mode': demo_mode_enabled(),
            'debug': bool(getattr(settings, 'DEBUG', False)),
        },
    }
    if include_logs:
        payload['recent_logs'] = recent_log_entries(limit=log_limit)
        payload['aggregated_logs'] = host_logs.get('entries', [])
    return payload


def build_diagnostics_bundle_files(*, log_limit: int = 500) -> dict[str, bytes]:
    """Multi-file bundle contents for tar.gz export."""
    payload = build_diagnostics_payload(include_logs=True, log_limit=log_limit)
    readme = f"""LabVault Diagnostics Bundle
Generated: {payload.get('generated_at')}
Host: {payload.get('hostname')}
Overall OK: {payload.get('ok')}

Files:
  summary.json       — overview + recommendations
  checks.json        — all pass/fail checks with remediation hints
  sections/          — databases, services, inventory, integrations
  recent_logs.json   — WARNING+ ring buffer (web process)
  logs/*.jsonl       — aggregated service logs (optional ingest)
  environment.json   — redacted LABVAULT_* env

Quick triage:
  1. checks.json — look for severity=fail
  2. sections/databases.json — postgres connection headroom
  3. recent_logs.json — OperationalError / too many clients
  4. recommendations in summary.json
"""
    files = {
        'README.txt': readme.encode('utf-8'),
        'summary.json': json.dumps({
            'ok': payload.get('ok'),
            'generated_at': payload.get('generated_at'),
            'hostname': payload.get('hostname'),
            'summary': payload.get('summary'),
            'recommendations': payload.get('recommendations'),
            'labvault': payload.get('labvault'),
        }, indent=2, default=str).encode('utf-8'),
        'checks.json': json.dumps(payload.get('checks'), indent=2, default=str).encode('utf-8'),
        'sections/databases.json': json.dumps(payload['sections']['databases'], indent=2, default=str).encode('utf-8'),
        'sections/services.json': json.dumps(payload['sections']['services'], indent=2, default=str).encode('utf-8'),
        'sections/inventory.json': json.dumps(payload['sections']['inventory'], indent=2, default=str).encode('utf-8'),
        'sections/integrations.json': json.dumps(payload['sections']['integrations'], indent=2, default=str).encode('utf-8'),
        'sections/platform.json': json.dumps(payload['sections']['platform'], indent=2, default=str).encode('utf-8'),
        'sections/api_smoke.json': json.dumps(payload['sections']['api_smoke'], indent=2, default=str).encode('utf-8'),
        'sections/host_logs.json': json.dumps(payload['sections'].get('host_logs'), indent=2, default=str).encode('utf-8'),
        'sections/workers.json': json.dumps(payload['sections'].get('workers'), indent=2, default=str).encode('utf-8'),
        'recent_logs.json': json.dumps(payload.get('recent_logs', []), indent=2, default=str).encode('utf-8'),
        'environment.json': json.dumps(payload.get('environment'), indent=2, default=str).encode('utf-8'),
        'full_report.json': json.dumps(payload, indent=2, default=str).encode('utf-8'),
    }
    files.update(host_log_bundle_files(limit=log_limit))
    return files


def build_diagnostics_tarball(*, log_limit: int = 500) -> bytes:
    """Return tar.gz bytes of the full diagnostics bundle."""
    files = build_diagnostics_bundle_files(log_limit=log_limit)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
