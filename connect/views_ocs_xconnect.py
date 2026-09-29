"""OCS crossconnect fleet APIs keyed by device management IP.

GET  /api/ocs/<device_ip>/crossconnects/
POST /api/ocs/<device_ip>/crossconnect/
     {"action": "xconnect_add"|"xconnect_delete",
      "port_a": "1.1.1", "port_b": "2.1.1", "name": "..."}

Only single add/delete is exposed here (no deleteall / bulk ops). A successful mutation
clears the device cache and schedules a background refresh so the device page and
``ocs-patch.json`` pick up the new state.
"""
from __future__ import annotations

import json
import re

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .keysight_views import _api_auth_required
from .models import Device


def _ocs_device(device_ip: str):
    dev = Device.objects.filter(ip_address=device_ip).first()
    if dev is None:
        return None, JsonResponse({"error": "ocs_device_not_found", "device_ip": device_ip}, status=404)
    vt = (dev.vendor_type or "").lower()
    if "ocs" not in vt and "calient" not in vt:
        return None, JsonResponse({"error": "not_an_ocs_device", "device_ip": device_ip, "vendor_type": vt}, status=400)
    return dev, None


def _xconnect_endpoints(row: dict) -> tuple[str, str, str]:
    port_a = str(row.get('inport') or row.get('in') or row.get('port_a') or '')
    port_b = str(row.get('outport') or row.get('out') or row.get('port_b') or '')
    state = str(row.get('oc') or row.get('state') or '').strip()
    h1 = row.get('half1') or {}
    h2 = row.get('half2') or {}
    if not (port_a and port_b) and isinstance(h1, dict):
        conn = str(h1.get('conn') or '')
        m = re.match(r'([^>]+)>([^>]+)', conn)
        if m:
            port_a, port_b = m.group(1).strip(), m.group(2).strip()
    half_states = []
    for half in (h1, h2):
        if isinstance(half, dict):
            hs = str(half.get('oc') or half.get('state') or '').strip()
            if hs:
                half_states.append(hs.upper())
    if not state and half_states:
        state = 'FAIL' if any(s == 'FAIL' for s in half_states) else half_states[0]
    elif state.upper() == 'OK' and any(s == 'FAIL' for s in half_states):
        state = 'FAIL'
    name = str(row.get('name') or row.get('connid') or row.get('conn') or '')
    return port_a, port_b, state or name


@_api_auth_required
def api_ocs_crossconnects_list(request, device_ip: str):
    """Live crossconnect list from the OCS REST API (``{ok, crossconnects, count, ...}``)."""
    dev, err = _ocs_device(device_ip)
    if err:
        return err
    from .drivers import get_driver

    try:
        driver = get_driver(dev)
        probe = driver.probe() if hasattr(driver, 'probe') else 'ok'
        if probe == 'auth_failed':
            return JsonResponse({
                'ok': False,
                'error': 'ocs_auth_failed',
                'detail': 'OCS REST rejected stored credentials (check Device username/password)',
                'device_ip': device_ip,
                'device_status': dev.status,
            }, status=502)
        rows = driver.fetch_crossconnect_list() or []
    except Exception as exc:
        return JsonResponse({"ok": False, "error": "ocs_fetch_failed", "detail": str(exc)}, status=502)
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        port_a, port_b, state = _xconnect_endpoints(r)
        out.append({
            "port_a": port_a,
            "port_b": port_b,
            "name": r.get("conn") or r.get("name") or r.get("connid") or "",
            "state": state,
            "raw": {k: v for k, v in r.items() if k not in ("password",)},
        })
    return JsonResponse({
        "ok": True,
        "crossconnects": out,
        "count": len(out),
        "device_ip": device_ip,
        "device_hostname": getattr(dev, 'hostname', None) or '',
    })


@_api_auth_required
def api_ocs_crossconnect_mutate(request, device_ip: str):
    """POST one ``xconnect_add`` / ``xconnect_delete`` to the OCS; blocked in demo mode."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    from .demo_mode import mutation_blocked_response
    blocked = mutation_blocked_response(request)
    if blocked:
        return blocked
    dev, err = _ocs_device(device_ip)
    if err:
        return err
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    action = str(body.get("action") or body.get("op") or "").strip().lower()
    port_a = str(body.get("port_a") or body.get("in") or "").strip()
    port_b = str(body.get("port_b") or body.get("out") or "").strip()
    name = str(body.get("name") or body.get("conn") or "").strip()
    if action not in ("xconnect_add", "xconnect_delete"):
        return JsonResponse({"error": "action must be xconnect_add|xconnect_delete"}, status=400)
    if action == "xconnect_add" and not (port_a and port_b):
        return JsonResponse({"error": "port_a and port_b required for add"}, status=400)
    if action == "xconnect_delete" and not (name or (port_a and port_b)):
        return JsonResponse({"error": "name or port pair required for delete"}, status=400)

    op = {"op": action, "in": port_a, "out": port_b}
    if name:
        op["conn"] = name

    from .drivers import get_driver

    try:
        driver = get_driver(dev)
        result = driver.send_config([json.dumps(op)])
    except Exception as exc:
        return JsonResponse({"error": "ocs_driver_failed", "detail": str(exc)}, status=502)
    if not getattr(result, "success", False):
        return JsonResponse(
            {"ok": False, "error": getattr(result, "error", "unknown"), "action": action},
            status=502,
        )
    from .views import _clear_cached_data, _schedule_device_data_refresh

    _clear_cached_data(dev.id)
    _schedule_device_data_refresh(dev)
    return JsonResponse({
        "ok": True,
        "action": action,
        "port_a": port_a,
        "port_b": port_b,
        "name": name,
        "result": getattr(result, "data", None),
    })


api_ocs_crossconnect_mutate = csrf_exempt(api_ocs_crossconnect_mutate)
