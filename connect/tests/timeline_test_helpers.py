"""Shared helpers for lab timeline / metric tests."""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable, List, Optional

from django.utils import timezone

from connect.models import (
    Device,
    KeysightChassis,
    LabMetricRollup,
    LabMetricSample,
    LabResourceEvent,
    LabTopology,
    LabTopologyNode,
)


def create_topology(name: str = 'test-topo') -> LabTopology:
    return LabTopology.objects.create(name=name)


def bulk_create_samples(
    topology_id: int,
    resource_key: str,
    metric: str,
    values: Iterable[float],
    *,
    start_at=None,
    step_minutes: int = 5,
) -> int:
    """Insert raw metric samples spaced by step_minutes."""
    now = start_at or timezone.now()
    vals = list(values)
    if not vals:
        return 0
    now = start_at or timezone.now()
    rows: List[LabMetricSample] = []
    for i, val in enumerate(vals):
        rows.append(LabMetricSample(
            topology_id=topology_id,
            resource_key=resource_key,
            metric=metric,
            value=float(val),
            sampled_at=now - timedelta(minutes=step_minutes * (len(vals) - 1 - i)),
        ))
    LabMetricSample.objects.using('np_timeseries').bulk_create(rows)
    return len(rows)


def create_rollup(
    topology_id: int,
    resource_key: str,
    metric: str,
    bucket_start,
    avg_value: float,
    *,
    max_value: Optional[float] = None,
    min_value: Optional[float] = None,
    sample_count: int = 12,
) -> LabMetricRollup:
    return LabMetricRollup.objects.using('np_timeseries').create(
        topology_id=topology_id,
        resource_key=resource_key,
        metric=metric,
        bucket_start=bucket_start,
        avg_value=avg_value,
        max_value=max_value if max_value is not None else avg_value,
        min_value=min_value if min_value is not None else avg_value,
        sample_count=sample_count,
    )


def create_chassis_node(topology: LabTopology, *, hostname: str = 'ixia1') -> LabTopologyNode:
    chassis = KeysightChassis.objects.create(
        hostname=hostname,
        ip_address='10.1.1.1',
        username='admin',
        password='admin',
        chassis_type='xgs12',
    )
    return LabTopologyNode.objects.create(
        topology=topology,
        node_type='chassis',
        label=hostname,
        extra={'chassis_id': chassis.pk},
    )


def create_switch_node(topology: LabTopology, *, hostname: str = 'sw1') -> LabTopologyNode:
    device = Device.objects.create(
        hostname=hostname,
        ip_address='10.2.2.2',
        vendor_type='arista',
    )
    return LabTopologyNode.objects.create(
        topology=topology,
        node_type='switch',
        label=hostname,
        device=device,
    )


def create_event(
    topology_id: int,
    resource_key: str,
    event_type: str,
    *,
    started_at=None,
    ended_at=None,
    payload=None,
) -> LabResourceEvent:
    return LabResourceEvent.objects.using('np_timeseries').create(
        topology_id=topology_id,
        resource_key=resource_key,
        event_type=event_type,
        started_at=started_at or timezone.now(),
        ended_at=ended_at,
        payload=payload or {},
    )
