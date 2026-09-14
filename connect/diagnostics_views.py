"""Diagnostics Center — staff UI and export endpoints."""
from __future__ import annotations

import json

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods

from connect.diagnostics import (
    build_diagnostics_payload,
    build_diagnostics_tarball,
)
from connect.keysight_views import _api_auth_required


def _parse_opts(request) -> dict:
    return {
        'include_logs': request.GET.get('logs', '1') != '0',
        'log_limit': min(1000, max(50, int(request.GET.get('log_limit', '500') or 500))),
    }


def _pretty_json(data) -> str:
    return json.dumps(data, indent=2, default=str)


@staff_member_required
@require_GET
def diagnostics_center(request):
    """Interactive Diagnostics Center page."""
    opts = _parse_opts(request)
    payload = build_diagnostics_payload(**opts)
    sections = payload.get('sections') or {}
    return render(request, 'connect/diagnostics_center.html', {
        'report': payload,
        'checks_json': _pretty_json(payload.get('checks', [])),
        'logs_json': _pretty_json(payload.get('recent_logs', [])),
        'json_heartbeat': _pretty_json(sections.get('services', {}).get('heartbeat')),
        'json_collector': _pretty_json(sections.get('services', {}).get('collector')),
        'json_ocs': _pretty_json(sections.get('integrations', {}).get('ocs')),
        'json_api_smoke': _pretty_json(sections.get('api_smoke')),
        'json_db_default': _pretty_json(sections.get('databases', {}).get('default')),
        'json_db_ts': _pretty_json(sections.get('databases', {}).get('np_timeseries')),
        'json_path_sizes': _pretty_json(sections.get('platform', {}).get('path_sizes')),
        'json_inventory': _pretty_json(sections.get('inventory')),
        'json_env': _pretty_json(payload.get('environment')),
        'json_workers': _pretty_json(sections.get('workers')),
        'json_host_logs': _pretty_json(sections.get('host_logs')),
    })


@staff_member_required
@require_GET
def diagnostics_json_live(request):
    """Live JSON for UI refresh."""
    return JsonResponse(build_diagnostics_payload(**_parse_opts(request)))


@staff_member_required
@require_GET
def diagnostics_export_page(request):
    """Quick single-file JSON download."""
    payload = build_diagnostics_payload(**_parse_opts(request))
    body = json.dumps(payload, indent=2, default=str)
    ts = payload.get('generated_at', 'now').replace(':', '-')[:19]
    resp = HttpResponse(body, content_type='application/json; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="labvault-diagnostics-{ts}.json"'
    return resp


@staff_member_required
@require_GET
def diagnostics_bundle_export(request):
    """Full multi-file tar.gz bundle for support / post-mortem."""
    opts = _parse_opts(request)
    data = build_diagnostics_tarball(log_limit=opts['log_limit'])
    ts = build_diagnostics_payload(include_logs=False)['generated_at'].replace(':', '-')[:19]
    resp = HttpResponse(data, content_type='application/gzip')
    resp['Content-Disposition'] = f'attachment; filename="labvault-diagnostics-bundle-{ts}.tar.gz"'
    return resp


@_api_auth_required
@require_GET
def diagnostics_export_api(request):
    """Bearer-token diagnostics JSON."""
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({'error': 'staff_required'}, status=403)
    if request.GET.get('bundle') == '1':
        opts = _parse_opts(request)
        data = build_diagnostics_tarball(log_limit=opts['log_limit'])
        ts = build_diagnostics_payload(include_logs=False)['generated_at'].replace(':', '-')[:19]
        resp = HttpResponse(data, content_type='application/gzip')
        resp['Content-Disposition'] = f'attachment; filename="labvault-diagnostics-bundle-{ts}.tar.gz"'
        return resp
    payload = build_diagnostics_payload(**_parse_opts(request))
    if request.GET.get('download') == '1':
        body = json.dumps(payload, indent=2, default=str)
        ts = payload.get('generated_at', 'now').replace(':', '-')[:19]
        resp = HttpResponse(body, content_type='application/json; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="labvault-diagnostics-{ts}.json"'
        return resp
    return JsonResponse(payload)


@require_http_methods(['POST'])
def diagnostics_log_ingest(request):
    """Optional log-agent push endpoint (shared secret)."""
    import os
    secret = os.environ.get('LABVAULT_DIAGNOSTICS_INGEST_SECRET', '').strip()
    if not secret or request.headers.get('X-LabVault-Diag-Secret') != secret:
        return JsonResponse({'error': 'forbidden'}, status=403)
    try:
        body = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid json'}, status=400)
    from connect.diagnostics_log_store import diagnostics_log_dir
    log_dir = diagnostics_log_dir()
    if log_dir is None:
        return JsonResponse({'error': 'log dir not configured'}, status=503)
    service = str(body.get('service') or 'unknown')
    path = log_dir / f'{service}.jsonl'
    entry = {
        'ts': body.get('ts', ''),
        'service': service,
        'level': body.get('level', 'INFO'),
        'message': str(body.get('message', ''))[:8000],
    }
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry) + '\n')
    return JsonResponse({'ok': True})
