"""Timeline and fabric API tests."""

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from connect.tests.timeline_test_helpers import create_topology


class TimelineApiTests(TestCase):
    databases = {'default', 'np_timeseries'}

    def setUp(self):
        cache.clear()
        self.topo = create_topology('api-test')
        self.client = Client()

    @patch('connect.lab_timeline_views.LabPortUsageGraphBuilder')
    def test_timeline_does_not_build_fabric(self, mock_builder):
        url = reverse('lab_topology_timeline_graph_json', args=[self.topo.pk])
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        mock_builder.assert_not_called()
        self.assertEqual(data['schema_version'], 2)
        self.assertIn('catalog', data)
        self.assertIn('metric_buckets', data)
        self.assertIn('events', data)
        self.assertIsInstance(data['events'], dict)

    @patch('connect.lab_timeline_views.LabPortUsageGraphBuilder')
    def test_fabric_endpoint_cached(self, mock_builder):
        instance = mock_builder.return_value
        instance.build.return_value = {
            'devices': [],
            'port_nodes': [],
            'port_links': [],
            'nodes': [],
            'meta': {},
        }
        url = reverse('lab_topology_fabric_graph_json', args=[self.topo.pk])
        res1 = self.client.get(url)
        res2 = self.client.get(url)
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res2.status_code, 200)
        mock_builder.assert_called_once()
        self.assertEqual(res1.json()['topology_id'], self.topo.pk)

    def test_timeline_window_params(self):
        now = timezone.now()
        frm = (now - timedelta(hours=6)).isoformat()
        to = now.isoformat()
        url = reverse('lab_topology_timeline_graph_json', args=[self.topo.pk])
        res = self.client.get(url, {'from': frm, 'to': to, 'bucket': '5m'})
        self.assertEqual(res.status_code, 200)
        window = res.json()['window']
        self.assertEqual(window['bucket'], '5m')

    @patch('connect.topology_resource_catalog._live_chassis_port_labels')
    def test_timeline_uses_fast_catalog_path(self, mock_live_ports):
        url = reverse('lab_topology_timeline_graph_json', args=[self.topo.pk])
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        mock_live_ports.assert_not_called()

    @patch('connect.lab_timeline_views.build_enriched_resource_catalog')
    def test_timeline_skips_full_timeseries_catalog_scan(self, mock_catalog):
        mock_catalog.return_value = {}
        url = reverse('lab_topology_timeline_graph_json', args=[self.topo.pk])
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        mock_catalog.assert_called()
        kwargs = mock_catalog.call_args.kwargs
        self.assertFalse(kwargs.get('observe_db', True))
        self.assertFalse(kwargs.get('live_ports', True))

    def test_timeline_metric_keys_match_catalog(self):
        from connect.topology_resource_catalog import build_enriched_resource_catalog

        catalog = build_enriched_resource_catalog(self.topo.pk, live_ports=False)
        url = reverse('lab_topology_timeline_graph_json', args=[self.topo.pk])
        data = self.client.get(url).json()
        allowed = set(catalog.keys())
        for key in data.get('metric_buckets', {}):
            self.assertIn(key, allowed, msg=f'orphan metric key {key!r}')
        for key in data.get('events', {}):
            self.assertIn(key, allowed, msg=f'orphan event key {key!r}')
