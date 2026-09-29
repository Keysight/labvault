"""Port usage episodes and 31-day cyclic aggregation (``np_timeseries``).

``PortUsageSample`` rows are interval events (owned, reserved, linked, patched).
``aggregate_usage_for_topology`` clips them to the last ``USAGE_WINDOW_DAYS`` (31)
and returns seconds-used per ``resource_key`` (``device_id__port_label``).
Reads and writes use the ``np_timeseries`` alias, not the default database.
See ``docs/development/subsystems/metrics-insights.md``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Tuple

from django.utils import timezone as django_tz

from .models import PortUsageSample

logger = logging.getLogger(__name__)

USAGE_WINDOW_DAYS = 31
USAGE_DB = 'np_timeseries'


def resource_key_for_port(device_id: str, port_label: str) -> str:
    """Stable key for a port on a topology device node."""
    label = (port_label or '').strip()
    return f'{device_id}__{label}' if label else str(device_id)


def _window_bounds(now=None) -> Tuple[datetime, datetime]:
    now = now or django_tz.now()
    start = now - timedelta(days=USAGE_WINDOW_DAYS)
    return start, now


def _clip_interval(
    started: datetime,
    ended: datetime,
    window_start: datetime,
    window_end: datetime,
) -> float:
    """Seconds of overlap between [started, ended] and [window_start, window_end]."""
    if ended <= window_start or started >= window_end:
        return 0.0
    lo = max(started, window_start)
    hi = min(ended, window_end)
    return max(0.0, (hi - lo).total_seconds())


def aggregate_usage_for_topology(
    topology_id: int,
    *,
    now=None,
) -> Dict[str, dict]:
    """
    Per-resource_key utilization over the last 31 days.

    Returns duty_cycle (0-1), heatmap_31d (31 daily duty ratios), episodes_31d, in_use.
    """
    window_start, window_end = _window_bounds(now)
    window_seconds = max(1.0, (window_end - window_start).total_seconds())
    day_seconds = 86400.0

    qs = (
        PortUsageSample.objects.using(USAGE_DB)
        .filter(topology_id=topology_id, started_at__lt=window_end)
        .exclude(ended_at__lt=window_start)
        .order_by('started_at')
    )

    by_key: Dict[str, dict] = {}
    for row in qs:
        key = row.resource_key
        entry = by_key.setdefault(
            key,
            {
                'resource_key': key,
                'port_label': row.port_label,
                'topology_node_id': row.topology_node_id,
                'chassis_id': row.chassis_id,
                'occupied_seconds': 0.0,
                'heatmap_seconds': [0.0] * USAGE_WINDOW_DAYS,
                'episodes_31d': 0,
                'in_use': False,
                'last_team': '',
                'last_user': '',
            },
        )
        started = row.started_at
        ended = row.ended_at or window_end
        if row.ended_at is None and row.event in ('enqueue', 'active', 'reservation'):
            entry['in_use'] = True
        entry['episodes_31d'] += 1
        if row.team:
            entry['last_team'] = row.team
        if row.user_name:
            entry['last_user'] = row.user_name

        overlap = _clip_interval(started, ended, window_start, window_end)
        entry['occupied_seconds'] += overlap

        for day_idx in range(USAGE_WINDOW_DAYS):
            day_start = window_start + timedelta(days=day_idx)
            day_end = day_start + timedelta(days=1)
            entry['heatmap_seconds'][day_idx] += _clip_interval(
                started, ended, day_start, day_end,
            )

    for entry in by_key.values():
        entry['duty_cycle'] = min(1.0, entry['occupied_seconds'] / window_seconds)
        entry['heatmap_31d'] = [
            min(1.0, sec / day_seconds) for sec in entry['heatmap_seconds']
        ]
        del entry['heatmap_seconds']
        del entry['occupied_seconds']

    return by_key


def _topology_metrics_collection_enabled(topology_id: int) -> bool:
    from connect.models import LabTopology

    return LabTopology.objects.filter(
        pk=topology_id, metrics_collection_enabled=True,
    ).exists()


def record_port_usage_episode(payload: dict) -> PortUsageSample:
    """
    Ingest one port-usage episode row (from an external scheduler or LabVault itself).

    Expected keys: topology_id, episode_id, event, resource_key, started_at;
    optional: ended_at, port_label, topology_node_id, chassis_id, ocs_triplet,
    team, user_name, source, meta. When ``resource_key`` is missing it is built from
    ``device_id``/``node_id`` + ``port_label``. Raises ``ValueError`` when the topology
    has metrics collection disabled.
    """
    topo_id = int(payload['topology_id'])
    if not _topology_metrics_collection_enabled(topo_id):
        raise ValueError(f'metrics collection disabled for topology {topo_id}')
    episode_id = str(payload['episode_id'])
    event = str(payload.get('event') or 'active')
    resource_key = str(payload.get('resource_key') or '')
    if not resource_key:
        device_id = payload.get('device_id') or payload.get('node_id') or ''
        port_label = payload.get('port_label') or payload.get('port') or ''
        resource_key = resource_key_for_port(str(device_id), str(port_label))

    started_raw = payload.get('started_at')
    if isinstance(started_raw, str):
        started_at = django_tz.datetime.fromisoformat(started_raw.replace('Z', '+00:00'))
        if django_tz.is_naive(started_at):
            started_at = django_tz.make_aware(started_at)
    else:
        started_at = started_raw or django_tz.now()

    ended_at = None
    ended_raw = payload.get('ended_at')
    if ended_raw:
        ended_at = django_tz.datetime.fromisoformat(str(ended_raw).replace('Z', '+00:00'))
        if django_tz.is_naive(ended_at):
            ended_at = django_tz.make_aware(ended_at)

    sample = PortUsageSample(
        topology_id=topo_id,
        topology_node_id=payload.get('topology_node_id'),
        resource_key=resource_key,
        port_label=str(payload.get('port_label') or payload.get('port') or ''),
        ocs_triplet=str(payload.get('ocs_triplet') or ''),
        chassis_id=payload.get('chassis_id'),
        episode_id=episode_id,
        event=event,
        team=str(payload.get('team') or payload.get('team_tag') or ''),
        user_name=str(payload.get('user_name') or payload.get('user') or ''),
        source=str(payload.get('source') or 'laas'),
        started_at=started_at,
        ended_at=ended_at,
        meta=dict(payload.get('meta') or {}),
    )
    sample.save(using=USAGE_DB)
    return sample


def close_episode(episode_id: str, *, ended_at=None, event: str = 'complete') -> int:
    """Set ended_at on open samples for an episode (returns update count)."""
    ended = ended_at or django_tz.now()
    return (
        PortUsageSample.objects.using(USAGE_DB)
        .filter(episode_id=episode_id, ended_at__isnull=True)
        .update(ended_at=ended, event=event)
    )


def ingest_episode_batch(items: Iterable[dict]) -> List[dict]:
    """Process a list of episode dicts; returns per-item status."""
    results = []
    for item in items:
        try:
            if item.get('action') == 'close':
                n = close_episode(
                    str(item['episode_id']),
                    ended_at=item.get('ended_at'),
                    event=str(item.get('event') or 'complete'),
                )
                results.append({'ok': True, 'closed': n, 'episode_id': item.get('episode_id')})
            else:
                row = record_port_usage_episode(item)
                results.append({'ok': True, 'id': row.pk, 'resource_key': row.resource_key})
        except ValueError as exc:
            if 'metrics collection disabled' in str(exc):
                results.append({
                    'ok': True,
                    'skipped': True,
                    'reason': str(exc),
                    'episode_id': item.get('episode_id'),
                })
            else:
                logger.warning('port usage ingest failed: %s', exc)
                results.append({'ok': False, 'error': str(exc), 'episode_id': item.get('episode_id')})
        except Exception as exc:
            logger.warning('port usage ingest failed: %s', exc)
            results.append({'ok': False, 'error': str(exc), 'episode_id': item.get('episode_id')})
    return results
