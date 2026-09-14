"""Tests for network topology draw.io / JSON export."""
from __future__ import annotations

from django.test import SimpleTestCase

from connect.topology_export import (
    filter_topology_subgraph,
    topology_export_json,
    topology_to_drawio_xml,
)


class TestTopologySubgraphFilter(SimpleTestCase):
    def test_tag_filter_keeps_connected_component(self):
        nodes = [
            {'id': 'a', 'name': 'A', 'tags': ['spine-1']},
            {'id': 'b', 'name': 'B', 'tags': []},
            {'id': 'c', 'name': 'C', 'tags': ['other']},
        ]
        links = [
            {'source': 'a', 'target': 'b'},
            {'source': 'c', 'target': 'c'},
        ]
        fn, fl = filter_topology_subgraph(nodes, links, ['spine-1'])
        ids = {n['id'] for n in fn}
        self.assertEqual(ids, {'a', 'b'})
        self.assertEqual(len(fl), 1)

    def test_empty_tags_returns_all(self):
        nodes = [{'id': 'x', 'tags': []}]
        links = []
        fn, fl = filter_topology_subgraph(nodes, links, None)
        self.assertEqual(len(fn), 1)
        self.assertEqual(fl, [])


class TestDrawioExport(SimpleTestCase):
    def test_drawio_xml_contains_vertices_and_edges(self):
        nodes = [
            {'id': 'sw1', 'name': 'Core', 'ip': '10.0.0.1', 'vendor': 'arista', 'status': 'online', 'tags': []},
            {'id': 'chassis-1', 'name': 'KS', 'ip': '10.0.0.2', 'vendor': 'keysight', 'status': 'online', 'tags': []},
        ]
        links = [
            {'source': 'sw1', 'target': 'chassis-1', 'local_port': 'Et1', 'remote_port': 'p1', 'status': 'up'},
        ]
        xml = topology_to_drawio_xml(nodes, links)
        self.assertIn('mxfile', xml)
        self.assertIn('mxGraphModel', xml)
        self.assertIn('Core', xml)
        self.assertIn('edge', xml)

    def test_json_export_schema(self):
        nodes = [{'id': 'a', 'name': 'A', 'vendor': 'arista', 'tags': ['t1']}]
        payload = topology_export_json(nodes, [], tags=['t1'])
        self.assertEqual(payload['format'], 'labvault-topology-export-v1')
        self.assertEqual(len(payload['nodes']), 1)
        self.assertIn('tier', payload['nodes'][0])
