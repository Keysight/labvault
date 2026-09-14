# HARD_DUMP_REMOVED — customer stub/compat
"""Bearer fleet APIs for the LabVault demo fork.

Every demo feature is reachable under /api/fleet/* (plus OCS routes wired in urls).
Auth: session cookie OR Authorization: Bearer <token> via _api_auth_required.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import timedelta

from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from connect.demo_mode import mutation_blocked_response
from connect.fleet_heartbeat import (
    fleet_heartbeat_payload,
    load_store,
    run_heartbeat_tick,
    seeded_mode,
)
from connect.keysight_views import _api_auth_required, _get_cached
from connect.models import (
    Device,
    KeysightChassis,
    KeysightChassisSnapshot,
    KeysightReservation,
    KeysightReservationItem,
    LabMetricSample,
    LabTopology,
)

logger = logging.getLogger(__name__)


def _parse_json(request) -> dict:
    if not request.body:
        return {}
    try:
        body = json.loads(request.body)
        return body if isinstance(body, dict) else {}
    except json.JSONDecodeError:
        return {}


def _active_reservations_qs():
    now = timezone.now()
    return KeysightReservation.objects.filter(
        status__in=('upcoming', 'active'),
        end_time__gte=now,
    ).prefetch_related('items', 'items__chassis', 'user')


def _reserved_port_keys() -> set[tuple[int, int | None, int | None]]:
    keys: set[tuple[int, int | None, int | None]] = set()
    for res in _active_reservations_qs():
        for item in res.items.all():
            keys.add((item.chassis_id, item.slot_number, item.port_number))
    return keys


def _port_is_reserved(chassis_id: int, slot, port, reserved_keys) -> bool:
    if (chassis_id, None, None) in reserved_keys:
        return True
    if slot is not None and (chassis_id, int(slot), None) in reserved_keys:
        return True
    if slot is not None and port is not None and (chassis_id, int(slot), int(port)) in reserved_keys:
        return True
    return False


def _hb_by_chassis() -> dict[int, dict]:
    payload = fleet_heartbeat_payload()
    out = {}
    for row in payload.get('chassis') or []:
        try:
            out[int(row['chassis_id'])] = row
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _cached_port_fields(cached: dict | None) -> dict[str, int | None]:
    """Port summary for fleet APIs; derive from raw ports when heartbeat omitted counts."""
    cached = cached or {}
    up = cached.get('ports_up')
    free = cached.get('ports_free')
    total = cached.get('total_ports')
    ports = cached.get('ports')
    if isinstance(ports, list):
        if total is None:
            total = len(ports)
        if up is None:
            up = sum(1 for p in ports if p.get('link_state') == 'up')
        if free is None:
            free = sum(1 for p in ports if p.get('owner', 'Free') == 'Free')
    return {'ports_up': up, 'ports_free': free, 'total_ports': total}


def _recovery_actions(ch: KeysightChassis, hb: dict | None) -> list[dict]:
    actions = []
    if hb and hb.get('halt_suspect'):
        actions.append({
            'action': 'power_cycle_chassis',
            'method': 'POST',
            'path': f'/api/fleet/chassis/{ch.pk}/recover',
            'body': {'action': 'reboot_chassis'},
            'hint': 'Chassis API stall / missed heartbeat — reboot chassis',
        })
        actions.append({
            'action': 'clear_and_reprobe',
            'method': 'GET',
            'path': f'/api/fleet/chassis/{ch.pk}/health.json?refresh=1',
            'hint': 'Force a fresh health probe',
        })
    if getattr(ch, 'hardware_error_reported', False):
        actions.append({
            'action': 'review_hw_error',
            'method': 'GET',
            'path': f'/keysight/chassis/{ch.pk}/',
            'hint': 'Hardware error flag set — review CN notes in UI',
        })
    if not actions:
        actions.append({
            'action': 'none',
            'hint': 'No automated recovery suggested',
        })
    return actions


@_api_auth_required
@require_GET
def fleet_index(request):
    endpoints = [
        {'method': 'GET', 'path': '/api/fleet/', 'story': 'meta'},
        {'method': 'GET', 'path': '/api/fleet/openapi.json', 'story': 'meta'},
        {'method': 'GET', 'path': '/api/docs/', 'story': 'meta'},
        {'method': 'GET', 'path': '/api/fleet/health.json', 'story': 'oncaller'},
        {'method': 'GET', 'path': '/api/fleet/heartbeat.json', 'story': 'oncaller'},
        {'method': 'GET', 'path': '/api/fleet/heartbeat/stream', 'story': 'oncaller'},
        {'method': 'GET', 'path': '/api/fleet/chassis/<id>/health.json', 'story': 'oncaller'},
        {'method': 'POST', 'path': '/api/fleet/chassis/<id>/recover', 'story': 'oncaller'},
        {'method': 'GET', 'path': '/api/fleet/ports/telemetry.json', 'story': 'test_user'},
        {'method': 'GET', 'path': '/api/fleet/ports/preflight.json', 'story': 'test_user'},
        {'method': 'GET', 'path': '/api/fleet/chassis/<id>/ports.json', 'story': 'test_user'},
        {'method': 'GET', 'path': '/api/fleet/sla.json', 'story': 'em_director'},
        {'method': 'GET', 'path': '/api/fleet/summary.json', 'story': 'em_director'},
        {'method': 'GET', 'path': '/api/fleet/metrics/transmission.json', 'story': 'em_director'},
        {'method': 'GET', 'path': '/api/fleet/inventory.json', 'story': 'lab_admin'},
        {'method': 'GET', 'path': '/api/fleet/inventory.csv', 'story': 'lab_admin'},
        {'method': 'GET', 'path': '/api/fleet/reservations.json', 'story': 'lab_admin'},
        {'method': 'POST', 'path': '/api/fleet/reservations', 'story': 'lab_admin'},
        {'method': 'POST', 'path': '/api/fleet/chassis/<id>/team-tags', 'story': 'lab_admin'},
        {'method': 'GET', 'path': '/api/fleet/ownership.json', 'story': 'infra_sre'},
        {'method': 'GET', 'path': '/api/fleet/conflicts.json', 'story': 'infra_sre'},
        {'method': 'GET', 'path': '/api/ocs/<device_ip>/crossconnects/', 'story': 'infra_sre'},
        {'method': 'POST', 'path': '/api/ocs/<device_ip>/crossconnect/', 'story': 'infra_sre'},
    ]
    return JsonResponse({
        'ok': True,
        'name': 'LabVault Fleet API',
        'auth': 'Authorization: Bearer <token> or session cookie',
        'keng_note': 'KENG maps to AresONE / IxOS chassis in this demo',
        'endpoints': endpoints,
    })


@require_GET
def fleet_openapi(request):
    """OpenAPI document — public so Swagger UI can load it; data endpoints stay Bearer-protected."""
    from connect.fleet_openapi import fleet_openapi_document
    # Prefer same-origin "/" so Swagger Try-it-out keeps the browser host:port
    # (e.g. :18001). Also advertise an absolute URL that preserves the port when
    # nginx forwards Host / X-Forwarded-Host correctly.
    host = (
        request.META.get('HTTP_X_FORWARDED_HOST')
        or request.META.get('HTTP_HOST')
        or request.get_host()
    )
    proto = request.META.get('HTTP_X_FORWARDED_PROTO') or request.scheme or 'http'
    absolute = f'{proto}://{host}'.rstrip('/')
    return JsonResponse(fleet_openapi_document(server_url='/', absolute_server_url=absolute))


@require_GET
def fleet_swagger_ui(request):
    """Interactive Swagger UI for fleet APIs (page is public; Try-it-out needs Bearer)."""
    from django.shortcuts import render
    return render(request, 'connect/fleet_swagger.html', {
        'openapi_url': '/api/fleet/openapi.json',
    })


@_api_auth_required
@require_GET
def fleet_health(request):
    hb_map = _hb_by_chassis()
    rows = []
    for ch in KeysightChassis.objects.all().order_by('pk'):
        hb = hb_map.get(ch.pk) or {}
        cached = _get_cached(ch.id) or {}
        port_fields = _cached_port_fields(cached)
        rows.append({
            'chassis_id': ch.pk,
            'hostname': ch.hostname or ch.ip_address,
            'ip_address': ch.ip_address,
            'chassis_type': ch.chassis_type,
            'status': ch.status,
            'last_seen': ch.last_seen.isoformat() if ch.last_seen else None,
            'team_tags': ch.team_tags or '',
            'hardware_error_reported': bool(getattr(ch, 'hardware_error_reported', False)),
            'cpu_pct': hb.get('cpu_pct') if hb.get('cpu_pct') is not None else cached.get('cpu_utilization'),
            'mem_pct': hb.get('mem_pct'),
            'heartbeat_ok': hb.get('heartbeat_ok'),
            'halt_suspect': hb.get('halt_suspect'),
            'halt_reason': hb.get('halt_reason', ''),
            'ports_up': port_fields['ports_up'],
            'ports_free': port_fields['ports_free'],
            'total_ports': port_fields['total_ports'],
            'recovery_actions': _recovery_actions(ch, hb),
            'source': hb.get('source') or 'keysight_cache',
        })
    return JsonResponse({
        'ok': True,
        'source': 'labvault_fleet_health',
        'generated_at': timezone.now().isoformat(),
        'chassis': rows,
        'counts': {
            'total': len(rows),
            'online': sum(1 for r in rows if r.get('status') == 'online'),
            'halt_suspect': sum(1 for r in rows if r.get('halt_suspect')),
        },
    })


@_api_auth_required
@require_GET
def fleet_heartbeat(request):
    if request.GET.get('tick') == '1':
        run_heartbeat_tick()
    return JsonResponse(fleet_heartbeat_payload())


@_api_auth_required
@require_GET
def fleet_heartbeat_stream(request):
    """SSE stream; clients may also poll /api/fleet/heartbeat.json every 2s."""
    import time as _time

    def event_stream():
        for _ in range(120):  # ~10 min at 5s
            if seeded_mode_safe():
                run_heartbeat_tick()
            payload = json.dumps(fleet_heartbeat_payload())
            yield f'event: heartbeat\ndata: {payload}\n\n'
            _time.sleep(heartbeat_interval_safe())

    resp = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    resp['Cache-Control'] = 'no-cache'
    resp['X-Accel-Buffering'] = 'no'
    return resp


def seeded_mode_safe() -> bool:
    from connect.fleet_heartbeat import seeded_mode
    return seeded_mode()


def heartbeat_interval_safe() -> int:
    from connect.fleet_heartbeat import heartbeat_interval_seconds
    return heartbeat_interval_seconds()


@_api_auth_required
@require_GET
def fleet_chassis_health(request, chassis_id: int):
    ch = get_object_or_404(KeysightChassis, pk=chassis_id)
    if request.GET.get('refresh') == '1':
        from connect.fleet_heartbeat import probe_chassis_live, seeded_mode, upsert_chassis_heartbeat, tick_seeded
        if seeded_mode():
            tick_seeded(KeysightChassis.objects.filter(pk=ch.pk))
        else:
            fields = probe_chassis_live(ch)
            fields['source'] = 'live'
            upsert_chassis_heartbeat(ch.pk, **fields)
    hb = _hb_by_chassis().get(ch.pk) or {}
    cached = _get_cached(ch.id) or {}
    port_fields = _cached_port_fields(cached)
    return JsonResponse({
        'ok': True,
        'chassis_id': ch.pk,
        'hostname': ch.hostname or ch.ip_address,
        'ip_address': ch.ip_address,
        'chassis_type': ch.chassis_type,
        'status': ch.status,
        'heartbeat': hb,
        'cached_summary': {
            'ports_up': port_fields['ports_up'],
            'ports_free': port_fields['ports_free'],
            'total_ports': port_fields['total_ports'],
            'cpu_utilization': cached.get('cpu_utilization'),
        },
        'pcpu': cached.get('pcpu_health') or cached.get('pcpu') or {},
        'recovery_actions': _recovery_actions(ch, hb),
        'source': 'labvault_chassis_health',
    })


@_api_auth_required
@require_http_methods(['POST'])
@csrf_exempt
def fleet_chassis_recover(request, chassis_id: int):
    blocked = mutation_blocked_response('recover')
    if blocked:
        return blocked
    ch = get_object_or_404(KeysightChassis, pk=chassis_id)
    body = _parse_json(request)
    action = (body.get('action') or request.POST.get('action') or 'reboot_chassis').strip()
    node_name = (body.get('node_name') or request.POST.get('node_name') or '').strip()

    from connect.keysight_drivers import get_driver
    driver = get_driver(ch)
    result = None
    try:
        if action in ('reboot_chassis', 'power_cycle_chassis') and hasattr(driver, 'reboot_chassis'):
            result = driver.reboot_chassis()
        elif action == 'power_cycle_node' and node_name and hasattr(driver, 'power_cycle_node'):
            result = driver.power_cycle_node(node_name)
        elif action == 'restart_node' and node_name and hasattr(driver, 'restart_node'):
            result = driver.restart_node(node_name)
        elif action == 'port_reboot':
            port_id = int(body.get('port_id') or 0)
            if not port_id:
                return JsonResponse({'ok': False, 'error': 'port_id required'}, status=400)
            result = driver.reboot_port(port_id)
        else:
            return JsonResponse({
                'ok': False,
                'error': 'unsupported_action',
                'action': action,
                'hint': 'Use reboot_chassis, power_cycle_node, restart_node, or port_reboot',
            }, status=400)
    except Exception as exc:
        return JsonResponse({'ok': False, 'error': str(exc), 'action': action}, status=502)

    ok = bool(getattr(result, 'success', False)) if result is not None else False
    return JsonResponse({
        'ok': ok,
        'action': action,
        'chassis_id': ch.pk,
        'error': getattr(result, 'error', None) if result else None,
        'data': getattr(result, 'data', None) if result else None,
    }, status=200 if ok else 502)


def _iter_ports_for_chassis(ch: KeysightChassis, reserved_keys) -> list[dict]:
    cached = _get_cached(ch.id) or {}
    ports = cached.get('ports') or []
    out = []
    for p in ports:
        if not isinstance(p, dict):
            continue
        slot = p.get('card_number') or p.get('slot') or p.get('slot_number')
        port = p.get('port_number') or p.get('port')
        try:
            slot_i = int(slot) if slot is not None else None
        except (TypeError, ValueError):
            slot_i = None
        try:
            port_i = int(port) if port is not None else None
        except (TypeError, ValueError):
            port_i = None
        owner = (p.get('owner') or 'Free').strip() or 'Free'
        link = p.get('link_state') or p.get('link') or p.get('status') or ''
        link_up = str(link).lower() in ('up', 'linkup', 'true', '1', 'connected')
        out.append({
            'chassis_id': ch.pk,
            'chassis_hostname': ch.hostname or ch.ip_address,
            'chassis_ip': ch.ip_address,
            'slot': slot_i,
            'port': port_i,
            'label': p.get('name') or p.get('label') or f'{slot_i}.{port_i}',
            'owner': owner,
            'link_up': link_up,
            'link_state': link,
            'speed': p.get('speed') or p.get('link_speed') or '',
            'reserved': _port_is_reserved(ch.pk, slot_i, port_i, reserved_keys),
            'hardware_error': bool(p.get('hardware_error_reported') or getattr(ch, 'hardware_error_reported', False)),
            'bps_in': p.get('bps_in') or p.get('rx_bps'),
            'bps_out': p.get('bps_out') or p.get('tx_bps'),
            'cpu_pct': p.get('cpu_pct'),
            'mem_pct': p.get('mem_pct'),
            'sample_age_s': None,
        })
    # Seeded fallback only in seeded heartbeat theater — never invent ports in live mode.
    if not out and seeded_mode():
        for slot in (1, 2):
            for port in range(1, 5):
                out.append({
                    'chassis_id': ch.pk,
                    'chassis_hostname': ch.hostname or ch.ip_address,
                    'chassis_ip': ch.ip_address,
                    'slot': slot,
                    'port': port,
                    'label': f'{slot}.{port}',
                    'owner': 'Free',
                    'link_up': True,
                    'link_state': 'up',
                    'speed': '100G',
                    'reserved': _port_is_reserved(ch.pk, slot, port, reserved_keys),
                    'hardware_error': bool(getattr(ch, 'hardware_error_reported', False)),
                    'bps_in': 0,
                    'bps_out': 0,
                    'cpu_pct': None,
                    'mem_pct': None,
                    'sample_age_s': 0,
                    'source': 'seeded_ports',
                })
    return out


@_api_auth_required
@require_GET
def fleet_ports_telemetry(request):
    reserved = _reserved_port_keys()
    ports = []
    for ch in KeysightChassis.objects.all().order_by('pk'):
        ports.extend(_iter_ports_for_chassis(ch, reserved))
    # Enrich bps from recent LabMetricSample when available
    try:
        since = timezone.now() - timedelta(hours=1)
        samples = (
            LabMetricSample.objects.filter(sampled_at__gte=since, metric__in=('bps_in', 'bps_out'))
            .order_by('-sampled_at')[:5000]
        )
        latest: dict[tuple[str, str], float] = {}
        for s in samples:
            key = (s.resource_key, s.metric)
            if key not in latest:
                latest[key] = float(s.value or 0)
        # Best-effort; leave seeded values if no match
        _ = latest
    except Exception:
        logger.debug('telemetry metric enrich skipped', exc_info=True)

    return JsonResponse({
        'ok': True,
        'source': 'labvault_port_telemetry',
        'generated_at': timezone.now().isoformat(),
        'count': len(ports),
        'ports': ports,
    })


@_api_auth_required
@require_GET
def fleet_ports_preflight(request):
    reserved = _reserved_port_keys()
    hb_map = _hb_by_chassis()
    topology_id = request.GET.get('topology_id')
    blockers = []
    ports_checked = 0
    chassis_rows = []

    for ch in KeysightChassis.objects.all().order_by('pk'):
        hb = hb_map.get(ch.pk) or {}
        chassis_api_ok = bool(hb.get('heartbeat_ok')) if hb else ch.status == 'online'
        halt_suspect = bool(hb.get('halt_suspect'))
        if halt_suspect or not chassis_api_ok:
            blockers.append({
                'type': 'chassis_api_stall' if halt_suspect else 'chassis_offline',
                'chassis_id': ch.pk,
                'hostname': ch.hostname or ch.ip_address,
                'detail': hb.get('halt_reason') or ch.status,
            })
        chassis_rows.append({
            'chassis_id': ch.pk,
            'hostname': ch.hostname or ch.ip_address,
            'chassis_api_ok': chassis_api_ok,
            'halt_suspect': halt_suspect,
            'halt_reason': hb.get('halt_reason', ''),
        })
        for p in _iter_ports_for_chassis(ch, reserved):
            ports_checked += 1
            if p.get('reserved'):
                blockers.append({
                    'type': 'port_reserved',
                    'chassis_id': ch.pk,
                    'label': p['label'],
                    'detail': 'Port is under an active LabVault reservation',
                })
            if not p.get('link_up'):
                blockers.append({
                    'type': 'link_down',
                    'chassis_id': ch.pk,
                    'label': p['label'],
                    'detail': p.get('link_state') or 'down',
                })
            if p.get('owner') and p['owner'] != 'Free':
                blockers.append({
                    'type': 'owned_by_other',
                    'chassis_id': ch.pk,
                    'label': p['label'],
                    'detail': f"owner={p['owner']}",
                })
            if p.get('hardware_error'):
                blockers.append({
                    'type': 'hardware_error',
                    'chassis_id': ch.pk,
                    'label': p['label'],
                    'detail': 'hardware_error_reported',
                })

    # Optional topology existence check
    topo_info = None
    if topology_id:
        try:
            topo = LabTopology.objects.get(pk=int(topology_id))
            topo_info = {'topology_id': topo.pk, 'name': topo.name}
        except (LabTopology.DoesNotExist, ValueError, TypeError):
            blockers.append({
                'type': 'topology_not_found',
                'detail': f'topology_id={topology_id}',
            })

    ok = len(blockers) == 0
    return JsonResponse({
        'ok': ok,
        'pass': ok,
        'source': 'labvault_preflight',
        'generated_at': timezone.now().isoformat(),
        'ports_checked': ports_checked,
        'blocker_count': len(blockers),
        'blockers': blockers,
        'chassis': chassis_rows,
        'topology': topo_info,
        'keng_note': 'KENG API deadlock mapped to chassis halt_suspect / heartbeat miss',
    }, status=200 if ok else 409)


@_api_auth_required
@require_GET
def fleet_chassis_ports(request, chassis_id: int):
    ch = get_object_or_404(KeysightChassis, pk=chassis_id)
    reserved = _reserved_port_keys()
    ports = _iter_ports_for_chassis(ch, reserved)
    return JsonResponse({
        'ok': True,
        'chassis_id': ch.pk,
        'hostname': ch.hostname or ch.ip_address,
        'count': len(ports),
        'ports': ports,
    })


@_api_auth_required
@require_GET
def fleet_sla(request):
    hours = int(request.GET.get('hours') or 24)
    since = timezone.now() - timedelta(hours=hours)
    rows = []
    live = not seeded_mode()
    for ch in KeysightChassis.objects.all().order_by('pk'):
        snaps = list(
            KeysightChassisSnapshot.objects.filter(chassis=ch, timestamp__gte=since)
            .order_by('timestamp')
            .values_list('timestamp', 'cpu_utilization')[:5000]
        )
        hb = _hb_by_chassis().get(ch.pk) or {}
        if live:
            # Live: host up (heartbeat_ok / icmp) is the truth; no fake 99.5%.
            if hb.get('heartbeat_ok'):
                uptime_pct = 100.0 if not hb.get('halt_suspect') else 80.0
            elif hb.get('icmp') or hb.get('open_ports'):
                uptime_pct = 50.0
            elif snaps:
                uptime_pct = 100.0
            else:
                uptime_pct = 0.0
            source = 'live_heartbeat'
        else:
            if snaps:
                uptime_pct = 100.0
            elif ch.status == 'online' and hb.get('heartbeat_ok'):
                uptime_pct = 99.5
            elif ch.status == 'online':
                uptime_pct = 95.0
            else:
                uptime_pct = 0.0
            if hb.get('halt_suspect'):
                uptime_pct = min(uptime_pct, 80.0)
            source = 'keysight_snapshots+heartbeat'
        rows.append({
            'chassis_id': ch.pk,
            'hostname': ch.hostname or ch.ip_address,
            'uptime_pct': uptime_pct,
            'status': ch.status,
            'snapshot_count': len(snaps),
            'halt_suspect': bool(hb.get('halt_suspect')),
            'heartbeat_ok': bool(hb.get('heartbeat_ok')),
            'icmp': bool(hb.get('icmp')),
            'api_probe': hb.get('api_probe') or '',
            'source': source,
        })
    fleet_avg = round(sum(r['uptime_pct'] for r in rows) / len(rows), 2) if rows else 0.0
    return JsonResponse({
        'ok': True,
        'source': 'labvault_fleet_sla',
        'mode': 'live' if live else 'seeded',
        'window_hours': hours,
        'fleet_uptime_pct': fleet_avg,
        'breach_count': sum(1 for r in rows if r['uptime_pct'] < 99.0),
        'chassis': rows,
        'generated_at': timezone.now().isoformat(),
    })


@_api_auth_required
@require_GET
def fleet_summary(request):
    reserved = _reserved_port_keys()
    hb_map = _hb_by_chassis()
    chassis_list = list(KeysightChassis.objects.all())
    ports = []
    for ch in chassis_list:
        ports.extend(_iter_ports_for_chassis(ch, reserved))
    bps_in = sum(float(p.get('bps_in') or 0) for p in ports)
    bps_out = sum(float(p.get('bps_out') or 0) for p in ports)
    return JsonResponse({
        'ok': True,
        'source': 'labvault_fleet_summary',
        'generated_at': timezone.now().isoformat(),
        'chassis_total': len(chassis_list),
        'chassis_online': sum(1 for c in chassis_list if c.status == 'online'),
        'halt_suspect': sum(1 for c in chassis_list if (hb_map.get(c.pk) or {}).get('halt_suspect')),
        'ports_total': len(ports),
        'ports_link_up': sum(1 for p in ports if p.get('link_up')),
        'ports_free': sum(1 for p in ports if p.get('owner') == 'Free' and not p.get('reserved')),
        'ports_reserved': sum(1 for p in ports if p.get('reserved')),
        'aggregate_bps_in': bps_in,
        'aggregate_bps_out': bps_out,
        'active_reservations': _active_reservations_qs().count(),
    })


@_api_auth_required
@require_GET
def fleet_transmission(request):
    window = (request.GET.get('window') or '1h').strip()
    hours = {'15m': 0.25, '1h': 1, '4h': 4, '24h': 24}.get(window, 1)
    since = timezone.now() - timedelta(hours=hours)
    series = []
    try:
        qs = (
            LabMetricSample.objects.filter(
                sampled_at__gte=since,
                metric__in=('bps_in', 'bps_out'),
            )
            .order_by('sampled_at')[:10000]
        )
        for s in qs:
            series.append({
                'resource_key': s.resource_key,
                'metric': s.metric,
                'value': float(s.value or 0),
                'sampled_at': s.sampled_at.isoformat() if s.sampled_at else None,
            })
    except Exception as exc:
        logger.debug('transmission query failed: %s', exc)

    if not series and seeded_mode():
        # Seeded synthetic series for demo theater only
        now = timezone.now()
        for i in range(12):
            ts = (now - timedelta(minutes=5 * (11 - i))).isoformat()
            series.append({
                'resource_key': 'seeded:chassis1:1.1',
                'metric': 'bps_in',
                'value': 1.2e9 + i * 1e7,
                'sampled_at': ts,
                'source': 'seeded',
            })
            series.append({
                'resource_key': 'seeded:chassis1:1.1',
                'metric': 'bps_out',
                'value': 9e8 + i * 8e6,
                'sampled_at': ts,
                'source': 'seeded',
            })

    return JsonResponse({
        'ok': True,
        'source': 'labvault_transmission',
        'mode': 'seeded' if seeded_mode() else 'live',
        'window': window,
        'count': len(series),
        'series': series,
        'generated_at': timezone.now().isoformat(),
    })


def _inventory_rows() -> list[dict]:
    reserved = _reserved_port_keys()
    hb_map = _hb_by_chassis()
    rows = []
    for ch in KeysightChassis.objects.all().order_by('pk'):
        ports = _iter_ports_for_chassis(ch, reserved)
        hb = hb_map.get(ch.pk) or {}
        rows.append({
            'chassis_id': ch.pk,
            'hostname': ch.hostname or ch.ip_address,
            'ip_address': ch.ip_address,
            'chassis_type': ch.chassis_type,
            'status': ch.status,
            'serial_number': ch.serial_number or '',
            'ixos_version': ch.ixos_version or '',
            'team_tags': ch.team_tags or '',
            'hardware_error_reported': bool(getattr(ch, 'hardware_error_reported', False)),
            'heartbeat_ok': hb.get('heartbeat_ok'),
            'halt_suspect': hb.get('halt_suspect'),
            'port_count': len(ports),
            'ports_free': sum(1 for p in ports if p.get('owner') == 'Free' and not p.get('reserved')),
            'ports_reserved': sum(1 for p in ports if p.get('reserved')),
            'ports': ports,
        })
    return rows


@_api_auth_required
@require_GET
def fleet_inventory(request):
    rows = _inventory_rows()
    return JsonResponse({
        'ok': True,
        'source': 'labvault_inventory',
        'count': len(rows),
        'chassis': rows,
        'generated_at': timezone.now().isoformat(),
    })


@_api_auth_required
@require_GET
def fleet_inventory_csv(request):
    rows = _inventory_rows()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        'chassis_id', 'hostname', 'ip_address', 'chassis_type', 'status',
        'team_tags', 'port_count', 'ports_free', 'ports_reserved', 'halt_suspect',
    ])
    for r in rows:
        w.writerow([
            r['chassis_id'], r['hostname'], r['ip_address'], r['chassis_type'], r['status'],
            r['team_tags'], r['port_count'], r['ports_free'], r['ports_reserved'], r['halt_suspect'],
        ])
    resp = HttpResponse(buf.getvalue(), content_type='text/csv')
    resp['Content-Disposition'] = 'attachment; filename="labvault_fleet_inventory.csv"'
    return resp


@_api_auth_required
@require_GET
def fleet_reservations(request):
    rows = []
    for res in _active_reservations_qs().order_by('start_time'):
        items = []
        for it in res.items.all():
            items.append({
                'chassis_id': it.chassis_id,
                'chassis': it.chassis.hostname or it.chassis.ip_address,
                'slot': it.slot_number,
                'port': it.port_number,
                'notes': it.notes,
            })
        rows.append({
            'id': res.pk,
            'title': res.title,
            'user': res.user.username,
            'status': res.status,
            'start_time': res.start_time.isoformat(),
            'end_time': res.end_time.isoformat(),
            'items': items,
        })
    return JsonResponse({
        'ok': True,
        'source': 'labvault_reservations',
        'count': len(rows),
        'reservations': rows,
    })


@_api_auth_required
@require_http_methods(['POST'])
@csrf_exempt
def fleet_reservations_create(request):
    blocked = mutation_blocked_response('create_reservation')
    # Allow reservation create in demo mode (planning feature); only block hardware mutate.
    # If you want to block this too, uncomment:
    # if blocked: return blocked
    _ = blocked
    body = _parse_json(request)
    title = (body.get('title') or '').strip() or 'Demo reservation'
    chassis_id = body.get('chassis_id')
    if not chassis_id:
        return JsonResponse({'ok': False, 'error': 'chassis_id required'}, status=400)
    ch = get_object_or_404(KeysightChassis, pk=int(chassis_id))
    hours = int(body.get('duration_hours') or 4)
    start = timezone.now()
    end = start + timedelta(hours=hours)
    res = KeysightReservation.objects.create(
        title=title,
        description=body.get('description') or 'Created via /api/fleet/reservations',
        user=request.user,
        start_time=start,
        end_time=end,
        status='active',
    )
    KeysightReservationItem.objects.create(
        reservation=res,
        chassis=ch,
        slot_number=body.get('slot'),
        port_number=body.get('port'),
        notes=body.get('notes') or '',
    )
    return JsonResponse({
        'ok': True,
        'reservation_id': res.pk,
        'title': res.title,
        'start_time': start.isoformat(),
        'end_time': end.isoformat(),
        'chassis_id': ch.pk,
    }, status=201)


@_api_auth_required
@require_http_methods(['POST'])
@csrf_exempt
def fleet_team_tags(request, chassis_id: int):
    ch = get_object_or_404(KeysightChassis, pk=chassis_id)
    body = _parse_json(request)
    tags_raw = body.get('team_tags')
    if tags_raw is None:
        tags_raw = request.POST.get('team_tags', '')
    if isinstance(tags_raw, list):
        ch.team_tags = ','.join(str(t).strip() for t in tags_raw if str(t).strip())
    else:
        ch.team_tags = ','.join(t.strip() for t in str(tags_raw).split(',') if t.strip())
    ch.save(update_fields=['team_tags'])
    return JsonResponse({'ok': True, 'chassis_id': ch.pk, 'team_tags': ch.team_tags})


@_api_auth_required
@require_GET
def fleet_ownership(request):
    reserved = _reserved_port_keys()
    rows = []
    for ch in KeysightChassis.objects.all().order_by('pk'):
        for p in _iter_ports_for_chassis(ch, reserved):
            rows.append({
                'chassis_id': p['chassis_id'],
                'chassis': p['chassis_hostname'],
                'label': p['label'],
                'ixos_owner': p['owner'],
                'reserved': p['reserved'],
                'team_tags': ch.team_tags or '',
                'link_up': p['link_up'],
            })
    return JsonResponse({
        'ok': True,
        'source': 'labvault_ownership',
        'count': len(rows),
        'ports': rows,
        'generated_at': timezone.now().isoformat(),
    })


@_api_auth_required
@require_GET
def fleet_conflicts(request):
    """Report overlapping reservations and owned+reserved collisions."""
    reserved = _reserved_port_keys()
    conflicts = []
    # Reservation overlaps (same chassis/slot/port, overlapping windows)
    items = list(
        KeysightReservationItem.objects.filter(
            reservation__status__in=('upcoming', 'active'),
            reservation__end_time__gte=timezone.now(),
        ).select_related('reservation', 'chassis', 'reservation__user')
    )
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if a.chassis_id != b.chassis_id:
                continue
            if a.reservation_id == b.reservation_id:
                continue
            same_scope = (
                (a.slot_number is None and a.port_number is None)
                or (b.slot_number is None and b.port_number is None)
                or (a.slot_number == b.slot_number and (
                    a.port_number is None or b.port_number is None or a.port_number == b.port_number
                ))
            )
            if not same_scope:
                continue
            ra, rb = a.reservation, b.reservation
            if ra.start_time < rb.end_time and rb.start_time < ra.end_time:
                conflicts.append({
                    'type': 'reservation_overlap',
                    'chassis_id': a.chassis_id,
                    'chassis': a.chassis.hostname or a.chassis.ip_address,
                    'slot': a.slot_number,
                    'port': a.port_number,
                    'reservation_a': {'id': ra.pk, 'title': ra.title, 'user': ra.user.username},
                    'reservation_b': {'id': rb.pk, 'title': rb.title, 'user': rb.user.username},
                })

    for ch in KeysightChassis.objects.all():
        for p in _iter_ports_for_chassis(ch, reserved):
            if p.get('reserved') and p.get('owner') and p['owner'] != 'Free':
                conflicts.append({
                    'type': 'owned_and_reserved',
                    'chassis_id': ch.pk,
                    'chassis': ch.hostname or ch.ip_address,
                    'label': p['label'],
                    'ixos_owner': p['owner'],
                    'detail': 'Port is both IxOS-owned and LabVault-reserved',
                })

    return JsonResponse({
        'ok': True,
        'source': 'labvault_conflicts',
        'count': len(conflicts),
        'conflicts': conflicts,
        'generated_at': timezone.now().isoformat(),
    })


# csrf_exempt wrappers for POST endpoints used with Bearer tokens
fleet_chassis_recover = csrf_exempt(fleet_chassis_recover)
fleet_reservations_create = csrf_exempt(fleet_reservations_create)
fleet_team_tags = csrf_exempt(fleet_team_tags)
