"""SQL aggregation, rollups, and get_metric_buckets dispatcher tests."""

from datetime import timedelta

from django.db import connections
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from connect.lab_metrics import (
    aggregate_from_rollups,
    aggregate_metric_buckets,
    aggregate_metric_buckets_sql,
    get_metric_buckets,
    rebuild_rollups_for_topology,
)
from connect.models import LabMetricSample
from connect.tests.timeline_test_helpers import (
    bulk_create_samples,
    create_rollup,
    create_topology,
)


class MetricAggregationTests(TestCase):
    databases = {'default', 'np_timeseries'}

    def setUp(self):
        self.topo = create_topology('agg-test')
        self.now = timezone.now()
        self.rkey = f'node_{self.topo.pk}'
        self.window_from = self.now - timedelta(hours=2)
        self.window_to = self.now + timedelta(minutes=1)

    def test_sql_matches_python_aggregation(self):
        bulk_create_samples(
            self.topo.pk, self.rkey, 'cpu_pct', [10.0, 20.0, 30.0],
            start_at=self.now, step_minutes=5,
        )
        py = aggregate_metric_buckets(
            self.topo.pk, self.window_from, self.window_to, bucket='5m',
        )
        sql = aggregate_metric_buckets_sql(
            self.topo.pk, self.window_from, self.window_to, bucket='5m',
        )
        self.assertEqual(
            py.get(self.rkey, {}).get('cpu_pct'),
            sql.get(self.rkey, {}).get('cpu_pct'),
        )

    def test_get_metric_buckets_short_window_uses_sql(self):
        bulk_create_samples(
            self.topo.pk, self.rkey, 'mem_pct', [40.0, 60.0],
            start_at=self.now, step_minutes=5,
        )
        with CaptureQueriesContext(connections['np_timeseries']) as ctx:
            buckets = get_metric_buckets(
                self.topo.pk, self.window_from, self.window_to, bucket='5m',
            )
        self.assertIn(self.rkey, buckets)
        self.assertLessEqual(len(ctx.captured_queries), 3)

    def test_long_window_reads_rollups_not_raw_scan(self):
        hour = self.now.replace(minute=0, second=0, microsecond=0)
        create_rollup(self.topo.pk, self.rkey, 'cpu_pct', hour - timedelta(hours=48), 55.0)
        create_rollup(
            self.topo.pk, self.rkey, 'cpu_pct',
            hour - timedelta(hours=47), 65.0,
        )
        long_from = self.now - timedelta(days=3)
        with CaptureQueriesContext(connections['np_timeseries']) as ctx:
            buckets = aggregate_from_rollups(
                self.topo.pk, long_from, self.window_to, bucket='1h',
            )
        self.assertIn(self.rkey, buckets)
        self.assertLessEqual(len(ctx.captured_queries), 2)
        self.assertGreaterEqual(len(buckets[self.rkey]['cpu_pct']), 2)

    def test_get_metric_buckets_dispatcher_switches_to_rollups(self):
        hour = self.now.replace(minute=0, second=0, microsecond=0)
        create_rollup(self.topo.pk, self.rkey, 'bps_in', hour - timedelta(hours=30), 1000.0)
        long_from = self.now - timedelta(days=2)
        long_to = self.now
        buckets = get_metric_buckets(self.topo.pk, long_from, long_to, bucket='5m')
        self.assertIn(self.rkey, buckets)
        self.assertIn('bps_in', buckets[self.rkey])

    def test_rebuild_rollups_from_raw(self):
        bulk_create_samples(
            self.topo.pk, self.rkey, 'cpu_pct', [10.0, 30.0],
            start_at=self.now, step_minutes=5,
        )
        n = rebuild_rollups_for_topology(
            self.topo.pk,
            window_from=self.window_from,
            window_to=self.window_to,
        )
        self.assertGreaterEqual(n, 1)
        hour = self.now.replace(minute=0, second=0, microsecond=0)
        rollups = aggregate_from_rollups(
            self.topo.pk, self.window_from, self.window_to, bucket='1h',
        )
        self.assertIn(self.rkey, rollups)
