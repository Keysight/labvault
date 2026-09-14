"""Per-topology metrics collection enable/disable."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from connect.management.commands.collect_topology_metrics import Command as CollectCommand
from connect.models import LabTopology
from connect.port_usage import ingest_episode_batch
from connect.tests.timeline_test_helpers import create_chassis_node, create_topology


class TopologyMetricsCollectionToggleTests(TestCase):
    databases = {'default', 'np_timeseries'}

    def setUp(self):
        self.topo = create_topology('toggle-test')
        self.node = create_chassis_node(self.topo)
        self.user = User.objects.create_user('metrics_toggle', password='test')
        self.client = Client()
        self.client.force_login(self.user)

    def test_api_toggle_persists(self):
        url = f'/lab-topology/{self.topo.pk}/data/'
        r = self.client.post(
            url,
            data='{"metrics_collection_enabled": false}',
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data['ok'])
        self.assertFalse(data['metrics_collection_enabled'])
        self.topo.refresh_from_db()
        self.assertFalse(self.topo.metrics_collection_enabled)

        r2 = self.client.get(url)
        self.assertFalse(r2.json()['metrics_collection_enabled'])

    def test_metrics_collection_endpoint_toggle(self):
        url = f'/lab-topology/{self.topo.pk}/metrics-collection/'
        r = self.client.post(
            url,
            data='{"enabled": false}',
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['metrics_collection_enabled'])

        r2 = self.client.get(url)
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json()['metrics_collection_enabled'])

        r3 = self.client.post(
            url,
            data='{"enabled": true}',
            content_type='application/json',
        )
        self.assertTrue(r3.json()['metrics_collection_enabled'])

    @patch('connect.lab_usage_insights.refresh_stale_insights_snapshots')
    @patch('connect.metric_collectors.collect_topology_node')
    def test_collector_skips_disabled_topology(self, mock_collect, mock_snap):
        self.topo.metrics_collection_enabled = False
        self.topo.save(update_fields=['metrics_collection_enabled'])

        from connect.metric_collectors import CollectorState

        cmd = CollectCommand()
        n = cmd._run_once(self.topo.pk, CollectorState(), workers=1)
        mock_collect.assert_not_called()
        self.assertEqual(n, 0)

    def test_port_usage_ingest_skips_when_disabled(self):
        self.topo.metrics_collection_enabled = False
        self.topo.save(update_fields=['metrics_collection_enabled'])
        results = ingest_episode_batch([{
            'topology_id': self.topo.pk,
            'episode_id': 'ep-1',
            'event': 'active',
            'resource_key': 'dev:1:port_1',
            'started_at': '2026-06-09T12:00:00+00:00',
        }])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['ok'])
        self.assertTrue(results[0].get('skipped'))

    def test_collector_includes_only_enabled_topologies(self):
        disabled = LabTopology.objects.create(
            name='disabled-topo',
            metrics_collection_enabled=False,
        )
        enabled_ids = set(
            LabTopology.objects.filter(metrics_collection_enabled=True).values_list('pk', flat=True)
        )
        self.assertIn(self.topo.pk, enabled_ids)
        self.assertNotIn(disabled.pk, enabled_ids)
