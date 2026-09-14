"""fetch_events_grouped tests."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from connect.lab_metrics import EVENTS_PER_RESOURCE_CAP, fetch_events_grouped
from connect.tests.timeline_test_helpers import create_event, create_topology


class EventGroupingTests(TestCase):
    databases = {'default', 'np_timeseries'}

    def setUp(self):
        self.topo = create_topology('events-test')
        self.now = timezone.now()
        self.window_from = self.now - timedelta(days=1)
        self.window_to = self.now + timedelta(minutes=1)

    def test_grouped_events_keyed_by_resource(self):
        rkey_a = 'node_1__1.1'
        rkey_b = 'node_1__1.2'
        create_event(self.topo.pk, rkey_a, 'link_up', started_at=self.now - timedelta(hours=2))
        create_event(self.topo.pk, rkey_b, 'port_owned', started_at=self.now - timedelta(hours=1))
        grouped = fetch_events_grouped(
            self.topo.pk, self.window_from, self.window_to,
        )
        self.assertIn(rkey_a, grouped)
        self.assertIn(rkey_b, grouped)
        self.assertEqual(grouped[rkey_a][0]['event_type'], 'link_up')

    def test_per_resource_cap_respected(self):
        rkey = 'node_1__2.1'
        cap = 5
        for i in range(cap + 10):
            create_event(
                self.topo.pk, rkey, 'link_up',
                started_at=self.now - timedelta(minutes=i),
            )
        grouped = fetch_events_grouped(
            self.topo.pk, self.window_from, self.window_to,
            per_resource_cap=cap,
        )
        self.assertLessEqual(len(grouped[rkey]), cap)
        self.assertEqual(EVENTS_PER_RESOURCE_CAP, 200)
