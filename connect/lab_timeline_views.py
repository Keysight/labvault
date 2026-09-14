"""Timeline graph API for lab topology resource usage."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from connect.cache_utils import cache_get, cache_set
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET

from connect.lab_metrics import BUCKET_SECONDS, fetch_events_grouped, get_metric_buckets
from connect.models import LabTopology
from connect.port_usage_graph import LabPortUsageGraphBuilder
from connect.topology_resource_catalog import (
    alias_switch_metric_buckets,
    apply_observed_keys_to_catalog,
    build_enriched_resource_catalog,
    catalog_resource_keys,
    restrict_events_grouped,
    restrict_metric_buckets,
)

logger = logging.getLogger(__name__)

FABRIC_CACHE_TTL = 90
TIMELINE_CACHE_TTL = 60


def _timeline_cache_key(topo_id: int, request) -> str:
    bucket = request.GET.get('bucket', '5m')
    if bucket not in BUCKET_SECONDS:
        bucket = '5m'
    window_from, window_to = _parse_window(request)
    digest = hashlib.md5(
        f'{window_from.isoformat()}|{window_to.isoformat()}|{bucket}'.encode()
    ).hexdigest()[:16]
    return f'lab_timeline_graph:{topo_id}:{digest}'


def _parse_window(request, default_hours: int = 4):
    now = timezone.now()
    raw_from = request.GET.get('from')
    raw_to = request.GET.get('to')
    window_to = parse_datetime(raw_to) if raw_to else now
    if window_to and timezone.is_naive(window_to):
        window_to = timezone.make_aware(window_to, timezone.get_current_timezone())
    if not window_to:
        window_to = now
    if raw_from:
        window_from = parse_datetime(raw_from)
        if window_from and timezone.is_naive(window_from):
            window_from = timezone.make_aware(window_from, timezone.get_current_timezone())
    else:
        window_from = window_to - timedelta(hours=default_hours)
    return window_from, window_to


def _fabric_cache_key(topo_id: int, request) -> str:
    flags = (
        request.GET.get('live', '1'),
        request.GET.get('lldp', '1'),
        request.GET.get('ocs', '1'),
        request.GET.get('planned', '1'),
        request.GET.get('reservations', '1'),
    )
    digest = hashlib.md5('|'.join(flags).encode()).hexdigest()[:12]
    return f'lab_fabric_graph:{topo_id}:{digest}'


def _build_fabric_graph(topo_id: int, request) -> dict:
    usage_graph = LabPortUsageGraphBuilder(topo_id).build(
        live=request.GET.get('live', '1') != '0',
        lldp=request.GET.get('lldp', '1') != '0',
        ocs=request.GET.get('ocs', '1') != '0',
        planned=request.GET.get('planned', '1') != '0',
        reservations=request.GET.get('reservations', '1') != '0',
    )
    return {
        'devices': usage_graph.get('devices') or [],
        'port_nodes': usage_graph.get('port_nodes') or [],
        'port_links': usage_graph.get('port_links') or [],
        'nodes': usage_graph.get('nodes') or [],
        'meta': usage_graph.get('meta') or {},
    }


@require_GET
def lab_topology_timeline_graph_json(request, topo_id: int):
    """GET /lab-topology/<id>/timeline-graph.json?from=&to=&bucket=5m

    Lightweight: catalog + metric buckets + grouped events only (no live fabric).
    Degrades gracefully: any aggregation error returns a valid empty payload
    instead of a 500 so the timeline UI can show an empty state.
    """
    get_object_or_404(LabTopology, pk=topo_id)

    bucket = request.GET.get('bucket', '5m')
    if bucket not in BUCKET_SECONDS:
        bucket = '5m'
    window_from, window_to = _parse_window(request)
    window = {
        'from': window_from.isoformat(),
        'to': window_to.isoformat(),
        'bucket': bucket,
    }

    cache_key = _timeline_cache_key(topo_id, request)
    cached = cache_get(cache_key)
    if cached is not None:
        return JsonResponse(cached)

    try:
        # Designer catalog only — do not DISTINCT the timeseries store here.
        # Observed ports are merged from the bucket/event keys already loaded.
        catalog = build_enriched_resource_catalog(
            topo_id, live_ports=False, observe_db=False,
        )
        raw_buckets = alias_switch_metric_buckets(
            get_metric_buckets(topo_id, window_from, window_to, bucket=bucket),
            topo_id,
        )
        events = fetch_events_grouped(topo_id, window_from, window_to)
        apply_observed_keys_to_catalog(
            catalog, list(raw_buckets.keys()) + list(events.keys()),
        )
        allowed = catalog_resource_keys(catalog)
        metric_buckets = restrict_metric_buckets(raw_buckets, allowed)
        events = restrict_events_grouped(events, allowed)
        payload = {
            'schema_version': 2,
            'topology_id': topo_id,
            'window': window,
            'catalog': catalog,
            'metric_buckets': metric_buckets,
            'events': events,
            'ok': True,
        }
        cache_set(cache_key, payload, TIMELINE_CACHE_TTL)
        return JsonResponse(payload)
    except Exception:  # noqa: BLE001 — never 500 the timeline; degrade to empty
        logger.exception('timeline-graph.json failed for topo %s', topo_id)
        return JsonResponse({
            'schema_version': 2,
            'topology_id': topo_id,
            'window': window,
            'catalog': {},
            'metric_buckets': {},
            'events': {},
            'ok': False,
            'error': 'Timeline data temporarily unavailable.',
        })


@require_GET
def lab_topology_fabric_graph_json(request, topo_id: int):
    """GET /lab-topology/<id>/fabric-graph.json — cached utilization fabric.

    A live-fabric build can fail (SSH/LLDP/OCS); on error we return a valid
    empty fabric so the topology canvas renders an empty state rather than 500.
    """
    get_object_or_404(LabTopology, pk=topo_id)

    cache_key = _fabric_cache_key(topo_id, request)
    cached = cache_get(cache_key)
    if cached is not None:
        return JsonResponse(cached)

    try:
        payload = {
            'schema_version': 1,
            'topology_id': topo_id,
            'ok': True,
            **_build_fabric_graph(topo_id, request),
        }
        cache_set(cache_key, payload, FABRIC_CACHE_TTL)
        return JsonResponse(payload)
    except Exception:  # noqa: BLE001 — degrade to empty fabric
        logger.exception('fabric-graph.json failed for topo %s', topo_id)
        return JsonResponse({
            'schema_version': 1,
            'topology_id': topo_id,
            'ok': False,
            'error': 'Fabric data temporarily unavailable.',
            'devices': [],
            'port_nodes': [],
            'port_links': [],
            'nodes': [],
            'meta': {},
        })
