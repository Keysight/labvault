"""Unit tests for canonical topology graph builder."""
from __future__ import annotations

import time
from unittest.mock import patch

from django.test import SimpleTestCase

from connect.topology_graph import (
    CACHE_TTL_SECONDS,
    TopologyGraphBuilder,
    build_global_graph,
    invalidate_cache,
    switch_ips_for_topology,
    topology_ids_for_switch_ips,
    validate_topology_payload,
)


class TestLinkHealthRules(SimpleTestCase):
    def test_up_when_fresh_lldp(self):
        now = time.time()
        h = TopologyGraphBuilder.link_health({'up': True, 'last_seen': now}, now=now)
        self.assertEqual(h, 'up')

    def test_stale_when_in_retention_window(self):
        now = time.time()
        h = TopologyGraphBuilder.link_health({'up': True, 'last_seen': now - 1200}, now=now)
        self.assertEqual(h, 'stale')

    def test_down_when_outside_retention(self):
        from connect.lldp_persistence import LLDP_RETENTION_SECONDS

        now = time.time()
        h = TopologyGraphBuilder.link_health(
            {'up': True, 'last_seen': now - LLDP_RETENTION_SECONDS - 10},
            now=now,
        )
        self.assertEqual(h, 'down')

    def test_planned_when_no_live(self):
        self.assertEqual(TopologyGraphBuilder.link_health(None), 'planned')

    def test_link_color_planned(self):
        self.assertEqual(TopologyGraphBuilder.link_color('planned'), '#64748b')


class TestMergeAlgorithm(SimpleTestCase):
    def test_dedup_by_endpoint_pair(self):
        builder = TopologyGraphBuilder(topo_id=1)
        raw = [
            {
                'id': 'plan_1',
                'source': 'node_1',
                'target': 'node_2',
                'port_a': 'Eth1',
                'port_b': 'Eth2',
                'provenance': ['planned'],
                '_priority': 6,
            },
            {
                'id': 'lldp_1',
                'source': 'node_1',
                'target': 'node_2',
                'port_a': 'Eth1',
                'port_b': 'Eth2',
                'type': 'lldp',
                'provenance': ['lldp_db'],
                '_priority': 2,
            },
        ]
        merged, conflicts = builder._merge_links(raw)
        self.assertEqual(len(merged), 1)
        self.assertIn('lldp_db', merged[0]['provenance'])
        self.assertIn('planned', merged[0]['provenance'])

    def test_lldp_wins_over_planned_for_same_endpoints(self):
        builder = TopologyGraphBuilder(topo_id=1)
        raw = [
            {
                'id': 'plan_1',
                'source': 'node_a',
                'target': 'node_b',
                'port_a': 'p1',
                'port_b': 'p2',
                'health': 'planned',
                'provenance': ['planned'],
                '_priority': 6,
            },
            {
                'id': 'lldp_1',
                'source': 'node_a',
                'target': 'node_b',
                'port_a': 'p1',
                'port_b': 'p2',
                'health': 'up',
                'type': 'lldp',
                'provenance': ['lldp_db'],
                '_priority': 2,
            },
        ]
        merged, _ = builder._merge_links(raw)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['id'], 'lldp_1')


class TestConflictDetection(SimpleTestCase):
    def test_port_mismatch_generates_conflict(self):
        builder = TopologyGraphBuilder(topo_id=1)
        planned = [
            {
                'id': 'plan_1',
                'source': 'node_1',
                'target': 'node_2',
                'port_a': 'Ethernet9',
                'port_b': 'swp1',
                'provenance': ['planned'],
            },
        ]
        live = [
            {
                'id': 'lldp_1',
                'source': 'node_1',
                'target': 'node_2',
                'port_a': 'Ethernet9',
                'port_b': 'Ethernet0',
                'type': 'lldp',
            },
        ]
        conflicts = builder._detect_conflicts(planned, live)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['reason'], 'planned_port_mismatch')

    def test_no_conflict_when_ports_match(self):
        builder = TopologyGraphBuilder(topo_id=1)
        planned = [{'id': 'p1', 'source': 'a', 'target': 'b', 'port_a': 'x', 'port_b': 'y', 'provenance': ['planned']}]
        live = [{'id': 'l1', 'source': 'a', 'target': 'b', 'port_a': 'x', 'port_b': 'y', 'type': 'lldp'}]
        self.assertEqual(builder._detect_conflicts(planned, live), [])


class TestCacheInvalidation(SimpleTestCase):
    def test_invalidate_by_topo_id(self):
        from connect.topology_graph import _cache_set, _graph_cache, _graph_cache_lock

        with _graph_cache_lock:
            _graph_cache.clear()
        _cache_set('20:1111', {'schema_version': 1})
        _cache_set('21:1111', {'schema_version': 1})
        invalidate_cache(20)
        with _graph_cache_lock:
            self.assertNotIn('20:1111', _graph_cache)
            self.assertIn('21:1111', _graph_cache)

    def test_cache_ttl_constant(self):
        self.assertEqual(CACHE_TTL_SECONDS, 30)


class TestLegacyAdapters(SimpleTestCase):
    def test_fabric_legacy_shape_unchanged(self):
        graph = {
            'topology_id': 20,
            'topo_name': 'HBG',
            'meta': {'generated_at': '2026-01-01T00:00:00+00:00'},
            'ocs_summary': {'total': 0, 'active': 0, 'alarm': 0, 'unmapped': 0, 'error': ''},
            'nodes': [
                {
                    'id': 'node_1',
                    'db_pk': 1,
                    'label': 'Arista1',
                    'mgmt_ipv4': '10.0.0.1',
                    'kind': 'switch',
                    'vendor': 'sonic',
                    'x': 0,
                    'y': 0,
                    'ports': [{'name': 'Ethernet1'}],
                    'status': 'unknown',
                    'device_id': 1,
                },
            ],
            'links': [
                {
                    'id': 'plan_1',
                    'type': 'planned',
                    'source': 'node_1',
                    'target': 'node_2',
                    'port_a': 'Eth1',
                    'port_b': 'Eth2',
                    'health': 'planned',
                    'color': '#64748b',
                    'label': '',
                    'ocs_triplets': [],
                },
            ],
        }
        legacy = TopologyGraphBuilder().to_fabric_legacy(graph)
        self.assertIn('nodes', legacy)
        self.assertIn('links', legacy)
        self.assertIn('ocs_summary', legacy)
        self.assertIn('fetched_at', legacy)
        self.assertEqual(legacy['nodes'][0]['ip'], '10.0.0.1')
        self.assertEqual(legacy['links'][0]['type'], 'planned')


class TestGlobalNetworkTopology(SimpleTestCase):
    """Network Topology page (/topology/) expects legacy D3 node/link fields."""

    @patch('connect.topology.get_cached_topology')
    def test_build_global_graph_matches_d3_schema(self, mock_cached):
        mock_cached.return_value = {
            'nodes': [{
                'id': '3',
                'ip': '10.0.0.3',
                'name': 'switch-a',
                'vendor': 'sonic',
                'status': 'online',
                'model': 'SN5600',
                'tags': ['team-a'],
            }],
            'links': [{
                'source': '3',
                'target': 'chassis-1',
                'local_port': 'Ethernet1',
                'remote_port': '1/1',
                'status': 'up',
                'link_count': 2,
            }],
        }
        graph = build_global_graph(lldp=True)
        self.assertEqual(len(graph['nodes']), 1)
        n = graph['nodes'][0]
        for key in ('id', 'ip', 'name', 'vendor', 'status', 'tags'):
            self.assertIn(key, n, msg=f'missing {key}')
        self.assertEqual(n['name'], 'switch-a')
        lk = graph['links'][0]
        for key in ('source', 'target', 'local_port', 'remote_port', 'status', 'link_count'):
            self.assertIn(key, lk, msg=f'missing link {key}')
        self.assertEqual(lk['link_count'], 2)


class TestScopedTopologyRefresh(SimpleTestCase):
    @patch('connect.topology_graph.LabTopologyNode.objects.filter')
    def test_switch_ips_for_topology_only_includes_switches(self, mock_filter):
        dev = type('D', (), {'ip_address': '10.0.0.1'})()
        n1 = type('N', (), {
            'node_type': 'switch', 'device_id': 1, 'device': dev, 'extra': {},
        })()
        n2 = type('N', (), {
            'node_type': 'chassis', 'device_id': None, 'device': None, 'extra': {},
        })()
        mock_filter.return_value.select_related.return_value = [n1, n2]
        self.assertEqual(switch_ips_for_topology(20), ['10.0.0.1'])

    def test_invalidate_cache_does_not_clear_other_topologies(self):
        from connect.topology_graph import _cache_set, _graph_cache, _graph_cache_lock

        with _graph_cache_lock:
            _graph_cache.clear()
        _cache_set('20:live', {'id': 20})
        _cache_set('22:live', {'id': 22})
        invalidate_cache(20)
        with _graph_cache_lock:
            self.assertNotIn('20:live', _graph_cache)
            self.assertIn('22:live', _graph_cache)


class TestValidatePayload(SimpleTestCase):
    @patch('connect.topology_graph.TopologyGraphBuilder.build')
    def test_validate_returns_ok_structure(self, mock_build):
        mock_build.return_value = {'conflicts': []}
        out = validate_topology_payload(20, {'nodes': [], 'links': []})
        self.assertIn('ok', out)
        self.assertIn('conflicts', out)
        self.assertIn('warnings', out)
