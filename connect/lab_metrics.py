"""Helpers for writing and querying lab topology metric samples and events.

All reads and writes go to the ``np_timeseries`` database (``TS_DB``):
``LabMetricSample`` (raw), ``LabMetricRollup`` (hourly), ``LabResourceEvent`` (intervals).
Bucket output shape is ``{resource_key: {metric: [[iso_ts, avg], ...]}}``.
``get_metric_buckets`` is the window-aware entry point used by Lab Pulse and the timeline.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.db import connections
from django.db.models import Q
from django.utils import timezone

from connect.models import LabMetricRollup, LabMetricSample, LabResourceEvent

TS_DB = 'np_timeseries'

BUCKET_SECONDS = {
    '5m': 300,
    '15m': 900,
    '1h': 3600,
}

# Default Lab Pulse window is 24h. Scanning raw samples for that window
# on a large timeseries store wedges gunicorn. Longer windows use hourly rollups.
RAW_WINDOW_HOURS = 4
EVENTS_PER_RESOURCE_CAP = 200
ROLLUP_BUCKET_SECONDS = 3600


def bulk_write_metrics(samples: Iterable[LabMetricSample]) -> int:
    """Insert raw samples into np_timeseries; returns the row count."""
    rows = list(samples)
    if not rows:
        return 0
    LabMetricSample.objects.using(TS_DB).bulk_create(rows, batch_size=500)
    return len(rows)


def bulk_write_events(events: Iterable[LabResourceEvent]) -> int:
    """Insert resource events into np_timeseries; returns the row count."""
    rows = list(events)
    if not rows:
        return 0
    LabResourceEvent.objects.using(TS_DB).bulk_create(rows, batch_size=500)
    return len(rows)


class EventWriteBuffer:
    """Accumulate resource events for a single collector cycle, then flush."""

    def __init__(self):
        self._pending: List[LabResourceEvent] = []
        self._close_ops: List[Tuple[int, str, str, datetime]] = []

    def open_event(
        self,
        topology_id: int,
        resource_key: str,
        event_type: str,
        started_at: Optional[datetime] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._pending.append(LabResourceEvent(
            topology_id=topology_id,
            resource_key=resource_key,
            event_type=event_type,
            started_at=started_at or timezone.now(),
            ended_at=None,
            payload=payload or {},
        ))

    def close_events(
        self,
        topology_id: int,
        resource_key: str,
        event_type: str,
        ended_at: Optional[datetime] = None,
    ) -> None:
        self._close_ops.append((
            topology_id,
            resource_key,
            event_type,
            ended_at or timezone.now(),
        ))

    def flush(self) -> Tuple[int, int]:
        closed = 0
        for topo_id, rkey, etype, ended in self._close_ops:
            closed += close_open_events(topo_id, rkey, etype, ended_at=ended)
        created = bulk_write_events(self._pending)
        self._pending.clear()
        self._close_ops.clear()
        return created, closed


def open_resource_event(
    topology_id: int,
    resource_key: str,
    event_type: str,
    started_at: Optional[datetime] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> LabResourceEvent:
    """Create one open (``ended_at=None``) event immediately (unbuffered)."""
    return LabResourceEvent.objects.using(TS_DB).create(
        topology_id=topology_id,
        resource_key=resource_key,
        event_type=event_type,
        started_at=started_at or timezone.now(),
        ended_at=None,
        payload=payload or {},
    )


def close_open_events(
    topology_id: int,
    resource_key: str,
    event_type: str,
    ended_at: Optional[datetime] = None,
) -> int:
    """Set ``ended_at`` on all open events matching topology/resource/type; returns count."""
    ended = ended_at or timezone.now()
    qs = LabResourceEvent.objects.using(TS_DB).filter(
        topology_id=topology_id,
        resource_key=resource_key,
        event_type=event_type,
        ended_at__isnull=True,
    )
    return qs.update(ended_at=ended)


def fetch_events(
    topology_id: int,
    window_from: datetime,
    window_to: datetime,
) -> List[Dict[str, Any]]:
    """All events overlapping ``[window_from, window_to)``, oldest first (uncapped)."""
    qs = LabResourceEvent.objects.using(TS_DB).filter(
        topology_id=topology_id,
        started_at__lt=window_to,
    ).filter(
        models_Q_ended_after(window_from),
    ).order_by('started_at')
    return [_event_to_dict(e) for e in qs]


def fetch_events_grouped(
    topology_id: int,
    window_from: datetime,
    window_to: datetime,
    *,
    per_resource_cap: int = EVENTS_PER_RESOURCE_CAP,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return events keyed by resource_key (newest first, capped per resource)."""
    lookback = window_from - timedelta(days=31)
    qs = LabResourceEvent.objects.using(TS_DB).filter(
        topology_id=topology_id,
        started_at__gte=lookback,
        started_at__lt=window_to,
    ).filter(
        models_Q_ended_after(window_from),
    ).order_by('-started_at')[:20000]

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ev in qs:
        bucket = grouped[ev.resource_key]
        if len(bucket) >= per_resource_cap:
            continue
        bucket.append(_event_to_dict(ev))
    for rkey in grouped:
        grouped[rkey].sort(key=lambda e: e['started_at'])
    return dict(grouped)


def _event_to_dict(e: LabResourceEvent) -> Dict[str, Any]:
    return {
        'resource_key': e.resource_key,
        'event_type': e.event_type,
        'started_at': e.started_at.isoformat(),
        'ended_at': e.ended_at.isoformat() if e.ended_at else None,
        'payload': e.payload or {},
    }


def models_Q_ended_after(window_from: datetime):
    """Q filter: event still open or ended after ``window_from``."""
    return Q(ended_at__isnull=True) | Q(ended_at__gt=window_from)


def _floor_bucket(ts: datetime, bucket_sec: int) -> datetime:
    epoch = int(ts.timestamp())
    floored = epoch - (epoch % bucket_sec)
    return datetime.fromtimestamp(floored, tz=ts.tzinfo)


def aggregate_metric_buckets(
    topology_id: int,
    window_from: datetime,
    window_to: datetime,
    bucket: str = '5m',
) -> Dict[str, Dict[str, List[List[Any]]]]:
    """Return { resource_key: { metric: [[iso_ts, avg_value], ...] } } (Python path)."""
    bucket_sec = BUCKET_SECONDS.get(bucket, 300)
    samples = LabMetricSample.objects.using(TS_DB).filter(
        topology_id=topology_id,
        sampled_at__gte=window_from,
        sampled_at__lt=window_to,
    ).values_list('resource_key', 'metric', 'value', 'sampled_at')

    buckets: Dict[str, Dict[str, Dict[datetime, List[float]]]] = {}
    for rkey, metric, value, sampled_at in samples:
        buckets.setdefault(rkey, {}).setdefault(metric, {})
        bts = _floor_bucket(sampled_at, bucket_sec)
        buckets[rkey][metric].setdefault(bts, []).append(float(value or 0))

    return _format_bucket_dict(buckets)


def _format_bucket_dict(
    buckets: Dict[str, Dict[str, Dict[datetime, List[float]]]],
) -> Dict[str, Dict[str, List[List[Any]]]]:
    out: Dict[str, Dict[str, List[List[Any]]]] = {}
    for rkey, metrics in buckets.items():
        out[rkey] = {}
        for metric, ts_map in metrics.items():
            series = []
            for bts in sorted(ts_map.keys()):
                vals = ts_map[bts]
                avg = sum(vals) / len(vals) if vals else 0.0
                if not math.isfinite(avg):
                    avg = 0.0
                series.append([bts.isoformat(), round(avg, 4)])
            out[rkey][metric] = series
    return out


def aggregate_metric_buckets_sql(
    topology_id: int,
    window_from: datetime,
    window_to: datetime,
    bucket: str = '5m',
) -> Dict[str, Dict[str, List[List[Any]]]]:
    """SQL-side bucketing over raw LabMetricSample rows."""
    bucket_sec = BUCKET_SECONDS.get(bucket, 300)
    conn = connections[TS_DB]
    if conn.vendor == 'sqlite':
        return _aggregate_sqlite(topology_id, window_from, window_to, bucket_sec)
    return _aggregate_orm_trunc(topology_id, window_from, window_to, bucket_sec)


def _aggregate_sqlite(
    topology_id: int,
    window_from: datetime,
    window_to: datetime,
    bucket_sec: int,
) -> Dict[str, Dict[str, List[List[Any]]]]:
    """Use epoch integer division for arbitrary bucket sizes on SQLite."""
    table = LabMetricSample._meta.db_table
    sql = f"""
        SELECT resource_key, metric,
               (CAST(strftime('%%s', sampled_at) AS INTEGER) / %s) * %s AS bucket_epoch,
               AVG(value) AS avg_value
        FROM {table}
        WHERE topology_id = %s
          AND sampled_at >= %s
          AND sampled_at < %s
        GROUP BY resource_key, metric, bucket_epoch
        ORDER BY bucket_epoch
    """
    params = [bucket_sec, bucket_sec, topology_id, window_from, window_to]
    out: Dict[str, Dict[str, List[List[Any]]]] = {}
    with connections[TS_DB].cursor() as cursor:
        cursor.execute(sql, params)
        for rkey, metric, bucket_epoch, avg_value in cursor.fetchall():
            bts = datetime.fromtimestamp(int(bucket_epoch), tz=window_from.tzinfo)
            out.setdefault(rkey, {}).setdefault(metric, []).append([
                bts.isoformat(),
                round(float(avg_value or 0), 4),
            ])
    return out


def _aggregate_orm_trunc(
    topology_id: int,
    window_from: datetime,
    window_to: datetime,
    bucket_sec: int,
) -> Dict[str, Dict[str, List[List[Any]]]]:
    """Postgres-friendly path: hourly rollups only when bucket is 1h; else Python."""
    if bucket_sec == 3600:
        return aggregate_from_rollups(topology_id, window_from, window_to, bucket='1h')
    return aggregate_metric_buckets(topology_id, window_from, window_to, bucket='5m')


def aggregate_from_rollups(
    topology_id: int,
    window_from: datetime,
    window_to: datetime,
    bucket: str = '1h',
) -> Dict[str, Dict[str, List[List[Any]]]]:
    """Read pre-aggregated hourly LabMetricRollup rows."""
    bucket_sec = BUCKET_SECONDS.get(bucket, 3600)
    qs = LabMetricRollup.objects.using(TS_DB).filter(
        topology_id=topology_id,
        bucket_start__gte=_floor_bucket(window_from, ROLLUP_BUCKET_SECONDS),
        bucket_start__lt=window_to,
    ).values_list('resource_key', 'metric', 'bucket_start', 'avg_value', 'max_value')

    if bucket_sec == ROLLUP_BUCKET_SECONDS:
        out: Dict[str, Dict[str, List[List[Any]]]] = {}
        for rkey, metric, bstart, avg_val, max_val in qs:
            out.setdefault(rkey, {}).setdefault(metric, []).append([
                bstart.isoformat(),
                round(float(avg_val or 0), 4),
            ])
        return out

    buckets: Dict[str, Dict[str, Dict[datetime, List[float]]]] = {}
    for rkey, metric, bstart, avg_val, _max_val in qs:
        buckets.setdefault(rkey, {}).setdefault(metric, {})
        bts = _floor_bucket(bstart, bucket_sec)
        buckets[rkey][metric].setdefault(bts, []).append(float(avg_val or 0))
    return _format_bucket_dict(buckets)


_LATEST_OVERLAY_METRICS = (
    'cpu_pct', 'mem_pct', 'bps_in', 'bps_out', 'port_ownership', 'link_speed_gbps',
)
LATEST_RAW_OVERLAY_MINUTES = 20


def overlay_latest_raw_samples(
    topology_id: int,
    buckets: Dict[str, Dict[str, List[List[Any]]]],
    *,
    lookback_minutes: int = LATEST_RAW_OVERLAY_MINUTES,
) -> Dict[str, Dict[str, List[List[Any]]]]:
    """Append the newest raw sample so 24h rollup Pulse tracks the live platform."""
    if not lookback_minutes:
        return buckets
    cutoff = timezone.now() - timedelta(minutes=max(1, int(lookback_minutes)))
    rows = (
        LabMetricSample.objects.using(TS_DB)
        .filter(
            topology_id=topology_id,
            sampled_at__gte=cutoff,
            metric__in=_LATEST_OVERLAY_METRICS,
        )
        .order_by('sampled_at')
        .values_list('resource_key', 'metric', 'sampled_at', 'value')
    )
    latest: Dict[Tuple[str, str], Tuple[datetime, float]] = {}
    for rkey, metric, ts, val in rows:
        latest[(rkey, metric)] = (ts, float(val or 0))
    for (rkey, metric), (ts, val) in latest.items():
        series = buckets.setdefault(rkey, {}).setdefault(metric, [])
        point = [ts.isoformat(), round(val, 4)]
        if series and str(series[-1][0]) >= point[0]:
            series[-1] = point
        else:
            series.append(point)
    return buckets


def get_metric_buckets(
    topology_id: int,
    window_from: datetime,
    window_to: datetime,
    bucket: str = '5m',
) -> Dict[str, Dict[str, List[List[Any]]]]:
    """Window-aware dispatcher: raw SQL for short windows, rollups for long."""
    hours = (window_to - window_from).total_seconds() / 3600.0
    if hours > RAW_WINDOW_HOURS:
        if bucket in ('5m', '15m'):
            bucket = '1h'
        rolled = aggregate_from_rollups(topology_id, window_from, window_to, bucket=bucket)
        if not rolled:
            rolled = aggregate_metric_buckets_sql(topology_id, window_from, window_to, bucket=bucket)
        return overlay_latest_raw_samples(topology_id, rolled)
    return aggregate_metric_buckets_sql(topology_id, window_from, window_to, bucket=bucket)


def upsert_hourly_rollups(
    topology_id: int,
    samples: Iterable[LabMetricSample],
) -> int:
    """Merge new raw samples into the current hour's LabMetricRollup rows."""
    hour_buckets: Dict[Tuple[str, str, datetime], List[float]] = defaultdict(list)
    for sample in samples:
        if sample.topology_id != topology_id:
            continue
        hour_start = _floor_bucket(sample.sampled_at, ROLLUP_BUCKET_SECONDS)
        key = (sample.resource_key, sample.metric, hour_start)
        hour_buckets[key].append(float(sample.value or 0))

    updated = 0
    for (rkey, metric, hour_start), vals in hour_buckets.items():
        if not vals:
            continue
        existing = LabMetricRollup.objects.using(TS_DB).filter(
            topology_id=topology_id,
            resource_key=rkey,
            metric=metric,
            bucket_start=hour_start,
        ).first()
        if existing:
            total_count = existing.sample_count + len(vals)
            weighted = (existing.avg_value * existing.sample_count) + sum(vals)
            new_avg = weighted / total_count if total_count else 0.0
            LabMetricRollup.objects.using(TS_DB).filter(pk=existing.pk).update(
                avg_value=round(new_avg, 4),
                max_value=max(existing.max_value, max(vals)),
                min_value=min(existing.min_value, min(vals)),
                sample_count=total_count,
            )
        else:
            LabMetricRollup.objects.using(TS_DB).create(
                topology_id=topology_id,
                resource_key=rkey,
                metric=metric,
                bucket_start=hour_start,
                avg_value=round(sum(vals) / len(vals), 4),
                max_value=max(vals),
                min_value=min(vals),
                sample_count=len(vals),
            )
        updated += 1
    return updated


def rebuild_rollups_for_topology(
    topology_id: int,
    window_from: Optional[datetime] = None,
    window_to: Optional[datetime] = None,
) -> int:
    """Rebuild hourly rollups from raw samples (backfill / repair)."""
    window_to = window_to or timezone.now()
    window_from = window_from or (window_to - timedelta(days=31))
    LabMetricRollup.objects.using(TS_DB).filter(
        topology_id=topology_id,
        bucket_start__gte=_floor_bucket(window_from, ROLLUP_BUCKET_SECONDS),
        bucket_start__lt=window_to,
    ).delete()

    samples = list(LabMetricSample.objects.using(TS_DB).filter(
        topology_id=topology_id,
        sampled_at__gte=window_from,
        sampled_at__lt=window_to,
    ))
    return upsert_hourly_rollups(topology_id, samples)
