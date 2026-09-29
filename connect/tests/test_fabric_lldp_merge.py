"""Port Fabric: merge IxOS LLDP neighbors onto fabric port groups."""
from django.test import SimpleTestCase

from connect.topology_device_ports import merge_lldp_into_fabric_port_groups


class FabricLldpMergeTests(SimpleTestCase):
    def test_merge_card_port_lldp(self):
        port_groups = [{
            'label': 'Card 1',
            'role': 'dac',
            'ports': [{
                'id': 'node_1__s1__c1p1',
                'label': 'c1p1',
                'slot': 1,
                'index': 1,
                'port_display': '1',
                'health': 'unused',
                'role': 'dac',
            }],
        }]
        neighbors = [{
            'local_port': '1/1',
            'remote_device': 'xgs12-bpsst',
            'remote_port': '1/2',
            'mgmt_ip': '198.18.2.160',
        }]
        n = merge_lldp_into_fabric_port_groups(port_groups, neighbors)
        self.assertEqual(n, 1)
        p = port_groups[0]['ports'][0]
        self.assertEqual(p['lldp_remote_device'], 'xgs12-bpsst')
        self.assertEqual(p['lldp_remote_port'], '1/2')
        self.assertEqual(p['health'], 'lldp_only')
