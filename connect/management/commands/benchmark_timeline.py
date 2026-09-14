"""Opt-in benchmark for timeline aggregation and API latency."""

from __future__ import annotations

import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from connect.lab_metrics import (
    aggregate_metric_buckets,
    aggregate_metric_buckets_sql,
    get_metric_buckets,
)
from connect.models import LabMetricSample, LabTopology
from connect.tests.timeline_test_helpers import create_topology


class Command(BaseCommand):
    help = 'Benchmark timeline aggregation and endpoints (opt-in, not for CI).'

    def add_arguments(self, parser):
        parser.add_argument('--samples', type=int, default=5000, help='Synthetic samples to insert')
        parser.add_argument('--topology-id', type=int, default=None)

    def handle(self, *args, **options):
        sample_count = max(100, int(options['samples']))
        topo = self._ensure_topology(options.get('topology_id'))
        self._seed_samples(topo.pk, sample_count)

        now = timezone.now()
        window_from = now - timedelta(days=7)
        window_to = now

        rows = [
            ('python aggregate_metric_buckets', lambda: aggregate_metric_buckets(
                topo.pk, window_from, window_to, bucket='5m',
            )),
            ('sql aggregate_metric_buckets_sql', lambda: aggregate_metric_buckets_sql(
                topo.pk, window_from, window_to, bucket='5m',
            )),
            ('dispatcher get_metric_buckets (7d)', lambda: get_metric_buckets(
                topo.pk, window_from, window_to, bucket='5m',
            )),
        ]
        self.stdout.write(self.style.MIGRATE_HEADING(f'Benchmark topology_id={topo.pk} samples={sample_count}'))
        for label, fn in rows:
            t0 = time.perf_counter()
            fn()
            ms = (time.perf_counter() - t0) * 1000
            self.stdout.write(f'  {label:40s} {ms:8.1f} ms')

        client = Client()
        for name in ('lab_topology_timeline_graph_json', 'lab_topology_fabric_graph_json'):
            url = reverse(name, args=[topo.pk])
            t0 = time.perf_counter()
            client.get(url)
            ms = (time.perf_counter() - t0) * 1000
            self.stdout.write(f'  HTTP {name:30s} {ms:8.1f} ms')

    def _ensure_topology(self, topo_id):
        if topo_id:
            return LabTopology.objects.get(pk=topo_id)
        return create_topology('benchmark-topo')

    def _seed_samples(self, topo_id: int, count: int) -> None:
        now = timezone.now()
        rows = []
        for i in range(count):
            rows.append(LabMetricSample(
                topology_id=topo_id,
                resource_key=f'node_{topo_id}__{i % 50 + 1}.{i % 8 + 1}',
                metric='bps_in' if i % 2 else 'cpu_pct',
                value=float(i % 100),
                sampled_at=now - timedelta(minutes=5 * (i % 200)),
            ))
        LabMetricSample.objects.using('np_timeseries').bulk_create(rows, batch_size=1000)
        self.stdout.write(f'Seeded {count} LabMetricSample rows')
