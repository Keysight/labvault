"""Lab Pulse — usage insights page and JSON API."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from connect.cache_utils import cache_get, cache_set
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from connect.keysight_views import _api_auth_required
from connect.lab_timeline_views import _parse_window
from connect.lab_usage_insights import (
    WINDOW_PRESETS,
    empty_insights_payload,
    read_insights_snapshot,
)
from connect.models import LabTopology

INSIGHTS_CACHE_TTL = 180
INSIGHTS_CACHE_VERSION = 'v11'


def _window_from_preset(preset: str):
    now = timezone.now()
    delta = WINDOW_PRESETS.get(preset, WINDOW_PRESETS['24h'])
    return now - delta, now


@login_required
@require_GET
def lab_topology_usage_insights_page(request, topo_id: int):
    """Lab Pulse — experimental multi-panel hardware usage insights."""
    from connect.lab_topology_split import get_sub_topology_nav

    topo = get_object_or_404(LabTopology, pk=topo_id)
    return render(request, 'connect/lab_topology_usage_insights.html', {
        'topo': topo,
        'sub_nav': get_sub_topology_nav(topo),
    })


@_api_auth_required
@require_GET
def lab_topology_usage_insights_json(request, topo_id: int):
    """GET /lab-topology/<id>/usage-insights.json?window=24h|4h|7d or from=&to=

    Auth: session cookie OR Bearer API token (LAAS optimizer is a service consumer).
    """
    get_object_or_404(LabTopology, pk=topo_id)

    preset = (request.GET.get('window') or '24h').strip().lower()
    if request.GET.get('from') or request.GET.get('to'):
        window_from, window_to = _parse_window(request, default_hours=24)
        cache_key = (
            f'usage_insights:{INSIGHTS_CACHE_VERSION}:{topo_id}:'
            f'{window_from.isoformat()}:{window_to.isoformat()}'
        )
    else:
        window_from, window_to = _window_from_preset(preset)
        cache_key = f'usage_insights:{INSIGHTS_CACHE_VERSION}:{topo_id}:preset:{preset}'

    cached = cache_get(cache_key)
    if cached is None and not (request.GET.get('from') or request.GET.get('to')):
        cached = read_insights_snapshot(topo_id, preset)
        if cached is not None:
            cache_set(cache_key, cached, INSIGHTS_CACHE_TTL)
    if cached is not None:
        resp = JsonResponse(cached)
        resp['Cache-Control'] = 'private, max-age=30'
        return resp

    # Do not scan the timeseries store on the web worker.
    payload = empty_insights_payload(topo_id, window_from, window_to)
    payload['ok'] = True
    payload['health']['reason'] = 'snapshot_warming'
    payload['health']['notes'] = [
        'Lab Pulse is refreshing from the metrics collector — reload in about a minute.',
    ]
    return JsonResponse(payload)
