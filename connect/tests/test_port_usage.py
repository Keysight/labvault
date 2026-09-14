"""Tests for port usage episodes and LabPortUsageGraphV1."""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from connect.models import PortUsageSample
from connect.port_usage import (
    USAGE_DB,
    aggregate_usage_for_topology,
    ingest_episode_batch,
    record_port_usage_episode,
    resource_key_for_port,
)
from connect.port_usage_graph import (
    _fabric_connection_resource_keys,
    _port_resource_key,
)


class TestResourceKey(TestCase):
    def test_resource_key_for_port(self):
        self.assertEqual(resource_key_for_port('node_1', '1.1'), 'node_1__1.1')

    def test_fabric_connection_resource_keys(self):
        sa, sb, pa, pb, rka, rkb = _fabric_connection_resource_keys({
            'src_device': 'node_1',
            'dst_device': 'node_2',
            'src_port': 'node_1__et-1/1',
            'dst_port': 'node_2__1.1',
        })
        self.assertEqual((sa, sb), ('node_1', 'node_2'))
        self.assertEqual((rka, rkb), ('node_1__et-1/1', 'node_2__1.1'))

        _, _, _, _, rka2, rkb2 = _fabric_connection_resource_keys({
            'src_device': '',
            'dst_device': '',
            'src_port': '',
            'dst_port': '',
        })
        self.assertEqual((rka2, rkb2), ('', ''))

    def test_port_resource_key_uses_metric_label(self):
        self.assertEqual(
            _port_resource_key('node_323', {
                'id': 'node_323__OCS_Uplink__2.1.1',
                'label': '2.1.1',
                'port_display': '2.1.1',
            }),
            'node_323__2.1.1',
        )
        self.assertEqual(
            _port_resource_key('node_1', {'id': 'node_1__s1__5', 'port_display': '5'}),
            'node_1__1.5',
        )
        self.assertEqual(
            _port_resource_key('node_1', {'label': '1.1'}),
            'node_1__1.1',
        )

    def test_fabric_connection_maps_to_metric_key(self):
        fmap = {'node_1__s1__5': 'node_1__1.5'}
        _, _, _, _, rka, rkb = _fabric_connection_resource_keys({
            'src_device': 'node_1',
            'dst_device': 'node_2',
            'src_port': 'node_1__s1__5',
            'dst_port': 'node_2__1.1',
        }, fmap)
        self.assertEqual(rka, 'node_1__1.5')
        self.assertEqual(rkb, 'node_2__1.1')


class TestPortUsageAggregation(TestCase):
    databases = {'default', 'np_timeseries'}

    def _enabled_topology(self, topo_id: int):
        from connect.models import LabTopology

        return LabTopology.objects.create(
            pk=topo_id,
            name=f'test-topo-{topo_id}',
            metrics_collection_enabled=True,
        )

    def test_duty_cycle_from_episode(self):
        topo_id = 999001
        self._enabled_topology(topo_id)
        now = timezone.now()
        started = now - timedelta(days=2)
        ended = now - timedelta(days=1)
        record_port_usage_episode({
            'topology_id': topo_id,
            'episode_id': 'ep-test-1',
            'event': 'active',
            'resource_key': 'node_x__2.1.1',
            'port_label': '2.1.1',
            'started_at': started.isoformat(),
            'ended_at': ended.isoformat(),
        })
        agg = aggregate_usage_for_topology(topo_id, now=now)
        self.assertIn('node_x__2.1.1', agg)
        self.assertGreater(agg['node_x__2.1.1']['duty_cycle'], 0)
        self.assertEqual(len(agg['node_x__2.1.1']['heatmap_31d']), 31)
        PortUsageSample.objects.using(USAGE_DB).filter(topology_id=topo_id).delete()

    def test_ingest_close_episode(self):
        topo_id = 999002
        self._enabled_topology(topo_id)
        now = timezone.now()
        ingest_episode_batch([{
            'topology_id': topo_id,
            'episode_id': 'ep-close-1',
            'event': 'active',
            'resource_key': 'node_y__1.1',
            'started_at': (now - timedelta(hours=1)).isoformat(),
        }])
        results = ingest_episode_batch([{
            'action': 'close',
            'episode_id': 'ep-close-1',
            'ended_at': now.isoformat(),
        }])
        self.assertTrue(results[0]['ok'])
        self.assertGreaterEqual(results[0].get('closed', 0), 1)
        PortUsageSample.objects.using(USAGE_DB).filter(topology_id=topo_id).delete()


class TestPortUsageUrls(TestCase):
    def test_usage_routes_resolve(self):
        self.assertIn('usage', reverse('lab_topology_usage', kwargs={'topo_id': 1}))
        self.assertIn('usage-graph.json', reverse('lab_topology_usage_graph_json', kwargs={'topo_id': 1}))
        self.assertIn('port-usage/episodes', reverse('api_port_usage_episodes'))
