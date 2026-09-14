"""API contract tests for topology graph endpoints (flags default off)."""
from __future__ import annotations

from django.test import SimpleTestCase


class TestTopologyFlagsDefaultOff(SimpleTestCase):
    def test_flags_default_off(self):
        from connect.topology_flags import (
            TOPOLOGY_GRAPH_FABRIC,
            TOPOLOGY_GRAPH_PORT_FABRIC,
            TOPOLOGY_GRAPH_V3,
        )

        self.assertFalse(TOPOLOGY_GRAPH_FABRIC)
        self.assertFalse(TOPOLOGY_GRAPH_PORT_FABRIC)
        self.assertFalse(TOPOLOGY_GRAPH_V3)


class TestGraphEndpointAuth(SimpleTestCase):
    def test_graph_json_url_resolves(self):
        from django.urls import reverse
        url = reverse('lab_topology_graph_json', kwargs={'topo_id': 20})
        self.assertIn('graph.json', url)
