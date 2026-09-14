"""LLDP neighbor → topology node resolution."""
from django.test import SimpleTestCase

from connect.topology_lldp_resolve import (
    build_hostname_to_node_map,
    lldp_hostname_aliases,
    resolve_lldp_neighbor_node_id,
)


class LldpHostnameAliasTests(SimpleTestCase):
    def test_compact_alias(self):
        aliases = list(lldp_hostname_aliases('AresOne-1.lab'))
        self.assertIn('aresone-1', aliases)
        self.assertIn('aresone1', aliases)


class ResolveLldpNeighborTests(SimpleTestCase):
    def test_mgmt_ip_resolves(self):
        ip_to_node = {'10.36.84.21': 'node_1'}
        nid = resolve_lldp_neighbor_node_id(
            {'mgmt_ip': '10.36.84.21', 'remote_device': 'other'},
            ip_to_node=ip_to_node,
            host_to_node={},
        )
        self.assertEqual(nid, 'node_1')

    def test_hostname_resolves(self):
        host_to_node = {'arista2': 'node_2'}
        nid = resolve_lldp_neighbor_node_id(
            {'remote_device': 'arista2.lbj.is.keysight.com'},
            ip_to_node={},
            host_to_node=host_to_node,
        )
        self.assertEqual(nid, 'node_2')

    def test_build_hostname_map_from_nodes(self):
        nodes = {
            'node_3': {'id': 'node_3', 'label': 'Arista3'},
        }
        m = build_hostname_to_node_map(nodes)
        self.assertEqual(m.get('arista3'), 'node_3')
