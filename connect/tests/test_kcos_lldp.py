"""Tests for KCOS compute-node LLDP collection (root SSH hop)."""

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from connect.kcos_ssh import parse_lldpcli_json, parse_lldpcli_text, resolve_kcos_ssh_key
from connect.keysight_drivers.ixos import DriverResult
from connect.keysight_drivers.kcos import KCOSDriver

_DICT_FORM = """
{
  "lldp": {
    "interface": {
      "eth2": {
        "via": "LLDP",
        "chassis": {
          "sonic-leaf1": {
            "id": {"type": "mac", "value": "aa:bb:cc:dd:ee:01"},
            "mgmt-ip": "198.18.3.95"
          }
        },
        "port": {
          "id": {"type": "ifname", "value": "Ethernet448"},
          "descr": "Eth57/1"
        }
      }
    }
  }
}
"""

_LIST_FORM = """
{
  "lldp": [{
    "interface": [{
      "name": "ens1f0",
      "chassis": [{
        "name": [{"value": "arista2"}],
        "id": [{"type": "mac", "value": "aa:bb:cc:dd:ee:02"}],
        "mgmt-ip": [{"value": "198.18.3.96"}]
      }],
      "port": [{
        "id": [{"type": "ifname", "value": "Ethernet12/1"}],
        "descr": [{"value": "leaf2 downlink"}]
      }]
    }]
  }]
}
"""


_LIST_KEYED_IFACE = """
{
  "lldp": [{
    "interface": [
      {"eaglefp0": {
        "via": "LLDP",
        "chassis": {"cn-1": {
          "id": {"type": "mac", "value": "aa:bb:cc:dd:ee:03"},
          "mgmt-ip": "172.16.0.11"
        }},
        "port": {"id": {"type": "ifname", "value": "eaglecp0"}, "descr": "eaglecp0"}
      }}
    ]
  }]
}
"""


_LLDPD_TEXT = """
-------------------------------------------------------------------------------
LLDP neighbors:
-------------------------------------------------------------------------------
Interface:    eaglefp0fo0, via: LLDP, RID: 1, Time: 0:01:00
  Chassis:
    ChassisID:    mac aa:bb:cc:dd:ee:ff
    SysName:      leaf-switch
    MgmtIP:       198.18.3.95
  Port:
    PortID:       ifname Ethernet16
    PortDescr:    uplink
"""


class ParseLldpcliTextTests(SimpleTestCase):
    def test_text_neighbor_block(self):
        rows = parse_lldpcli_text(_LLDPD_TEXT)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['interface'], 'eaglefp0fo0')
        self.assertEqual(r['remote_device'], 'leaf-switch')
        self.assertEqual(r['remote_port'], 'Ethernet16')
        self.assertEqual(r['mgmt_ip'], '198.18.3.95')

    def test_non_text_returns_empty(self):
        self.assertEqual(parse_lldpcli_text(''), [])
        self.assertEqual(parse_lldpcli_text('no neighbors'), [])


class ParseLldpcliJsonTests(SimpleTestCase):
    def test_dict_keyed_form(self):
        rows = parse_lldpcli_json(_DICT_FORM)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['interface'], 'eth2')
        self.assertEqual(r['remote_device'], 'sonic-leaf1')
        self.assertEqual(r['remote_port'], 'Ethernet448')
        self.assertEqual(r['chassis_id'], 'aa:bb:cc:dd:ee:01')
        self.assertEqual(r['mgmt_ip'], '198.18.3.95')

    def test_list_form(self):
        rows = parse_lldpcli_json(_LIST_FORM)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['interface'], 'ens1f0')
        self.assertEqual(r['remote_device'], 'arista2')
        self.assertEqual(r['remote_port'], 'Ethernet12/1')
        self.assertEqual(r['mgmt_ip'], '198.18.3.96')

    def test_list_keyed_interface_form(self):
        rows = parse_lldpcli_json(_LIST_KEYED_IFACE)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['interface'], 'eaglefp0')
        self.assertEqual(r['remote_device'], 'cn-1')
        self.assertEqual(r['remote_port'], 'eaglecp0')
        self.assertEqual(r['mgmt_ip'], '172.16.0.11')

    def test_non_json_returns_empty(self):
        self.assertEqual(parse_lldpcli_json(''), [])
        self.assertEqual(parse_lldpcli_json('lldpcli: command not found'), [])
        self.assertEqual(parse_lldpcli_json('{"lldp": {}}'), [])


class ResolveKcosSshKeyTests(SimpleTestCase):
    def test_ppk_uses_openssh_sibling(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ppk = base / 'operator.ppk'
            sibling = base / 'operator.key'
            ppk.write_text('putty-placeholder', encoding='utf-8')
            sibling.write_text('openssh-placeholder', encoding='utf-8')
            self.assertEqual(resolve_kcos_ssh_key(str(ppk)), str(sibling))

    def test_openssh_path_passthrough(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'operator.key'
            key.write_text('openssh-placeholder', encoding='utf-8')
            self.assertEqual(resolve_kcos_ssh_key(str(key)), str(key))

    def test_missing_path_empty(self):
        self.assertEqual(resolve_kcos_ssh_key('/no/such/operator.key'), '')


class KcosDriverLldpSshTests(TestCase):
    @override_settings(KCOS_ROOT_SSH_PASSWORD='pw', KCOS_ROOT_SSH_KEY='')
    def test_get_lldp_ssh_maps_interface_to_slot_port_label(self):
        driver = KCOSDriver(ip='198.18.1.233', username='u', password='p')
        nodes_payload = DriverResult(success=True, data=[
            {'name': 'merlin-node', 'role': 'merlin', 'internalIP': '172.16.0.1'},
            {'name': 'compute-1', 'role': 'worker', 'internalIP': '172.16.0.11'},
        ])
        ports_payload = DriverResult(success=True, data=[
            {'node_name': 'merlin-node', 'from_interface': 'eth0', 'card_number': 0, 'port_number': 1},
            {'node_name': 'compute-1', 'from_interface': 'eth2', 'card_number': 3, 'port_number': 1},
        ])
        per_node = {
            'merlin-node': [
                {'interface': 'eth0', 'remote_device': 'leaf-a',
                 'remote_port': 'Ethernet1', 'chassis_id': 'aa:aa', 'mgmt_ip': '198.18.3.95'},
            ],
            'compute-1': [
                {'interface': 'eth2', 'remote_device': 'sonic-leaf1',
                 'remote_port': 'Ethernet448', 'chassis_id': 'aa:bb', 'mgmt_ip': '198.18.3.95'},
                {'interface': 'ethX', 'remote_device': 'mystery',
                 'remote_port': 'p1', 'chassis_id': '', 'mgmt_ip': ''},
            ],
        }
        with patch.object(driver, '_get', return_value=nodes_payload), \
                patch.object(driver, 'get_ports', return_value=ports_payload), \
                patch('connect.kcos_ssh.collect_kcos_lldp', return_value=per_node) as mock_collect:
            res = driver.get_lldp_ssh()

        self.assertTrue(res.success)
        by_iface = {r['interface']: r for r in res.data}
        # Mapped interface gets the slot.port label the topology map expects.
        self.assertEqual(by_iface['eth2']['local_port'], '3.1')
        self.assertEqual(by_iface['eth2']['remote_device'], 'sonic-leaf1')
        # Merlin front-panel port mapped to slot.port.
        self.assertEqual(by_iface['eth0']['local_port'], '0.1')
        # Unmapped interface falls back to node:iface (still visible, never lost).
        self.assertEqual(by_iface['ethX']['local_port'], 'compute-1:ethX')
        sent_nodes = mock_collect.call_args.args[1]
        self.assertEqual([n['name'] for n in sent_nodes], ['compute-1'])
        self.assertEqual(mock_collect.call_args.kwargs['merlin_node'], {'name': 'merlin-node'})
        self.assertFalse(mock_collect.call_args.kwargs['probe_merlin'])
        self.assertTrue(mock_collect.call_args.kwargs['probe_compute'])
        self.assertIn('node_interfaces', mock_collect.call_args.kwargs)

    @override_settings(KCOS_ROOT_SSH_PASSWORD='pw', KCOS_ROOT_SSH_KEY='')
    def test_m8400_producer_pods_no_compute_hops(self):
        driver = KCOSDriver(ip='203.0.113.37', username='u', password='p')
        nodes_payload = DriverResult(success=True, data=[
            {'name': 'mgmt', 'role': 'merlin', 'internalIP': '172.16.0.1'},
            {'name': 'cn-1', 'role': 'worker', 'internalIP': '172.16.0.11'},
        ])
        bps_topology = {
            'slots': [{
                'id': 1,
                'physical_ports': [{
                    'id': 3,
                    'currentMode': '4x100G-PAM4',
                    'lanes': [{'id': '3.0'}, {'id': '3.1'}],
                }],
            }],
        }
        ports_payload = DriverResult(success=True, data=[
            {
                'node_name': 'cn-1',
                'from_interface': 'eaglefp2fo0',
                'card_number': 3,
                'port_number': 1,
                'to_switch_port': '3.0',
                'link_state': 'UP',
            },
        ])
        per_node = {
            'cn-1': [
                {'interface': 'eaglefp2fo0', 'remote_device': 'leaf-a',
                 'remote_port': 'Ethernet1', 'chassis_id': 'aa:aa', 'mgmt_ip': '198.18.3.95'},
            ],
        }
        with patch.object(driver, '_get', return_value=nodes_payload), \
                patch.object(driver, 'get_ports', return_value=ports_payload), \
                patch('connect.kcos_ssh.collect_kcos_lldp', return_value=per_node) as mock_collect:
            res = driver.get_lldp_ssh(bps_topology=bps_topology, chassis_type='aps_m8400')

        self.assertTrue(res.success)
        self.assertEqual(res.data[0]['local_port'], '3.2')
        self.assertEqual(mock_collect.call_args.args[1], [])
        kwargs = mock_collect.call_args.kwargs
        self.assertFalse(kwargs['probe_merlin'])
        self.assertFalse(kwargs['probe_compute'])
        self.assertTrue(kwargs['probe_producer_pods'])
        self.assertFalse(kwargs['merlin_use_k8s_lldpd'])
        self.assertEqual(kwargs['node_interfaces']['cn-1'], ['eaglefp2fo0'])

    @override_settings(KCOS_ROOT_SSH_PASSWORD='pw', KCOS_ROOT_SSH_KEY='')
    def test_m8400_b2b_synthesis_when_ssh_lldp_empty(self):
        driver = KCOSDriver(ip='203.0.113.37', username='u', password='p')
        nodes_payload = DriverResult(success=True, data=[
            {'name': 'mgmt', 'role': 'merlin', 'internalIP': '172.16.0.1'},
        ])
        ports_payload = DriverResult(success=True, data=[
            {
                'node_name': 'cn-a',
                'from_interface': 'eaglefp0fo0',
                'card_number': 1,
                'port_number': 4,
                'to_switch_port': '13.4',
                'link_state': 'UP',
                'transceiver_serial': 'SN123',
            },
            {
                'node_name': 'cn-a',
                'from_interface': 'eaglefp2fo0',
                'card_number': 1,
                'port_number': 6,
                'to_switch_port': '13.6',
                'link_state': 'UP',
                'transceiver_serial': 'SN123',
            },
        ])
        hostname_payload = DriverResult(success=True, data={'name': 'merpro2c'})
        with patch.object(driver, '_get', return_value=nodes_payload), \
                patch.object(driver, 'get_ports', return_value=ports_payload), \
                patch.object(driver, 'get_hostname', return_value=hostname_payload), \
                patch('connect.kcos_ssh.collect_kcos_lldp', return_value={}) as mock_collect:
            res = driver.get_lldp_ssh(bps_topology={'slots': []}, chassis_type='aps_m8400')

        self.assertTrue(res.success)
        self.assertEqual(len(res.data), 2)
        locals_ = {r['local_port'] for r in res.data}
        self.assertEqual(locals_, {'1.0', '1.2'})
        self.assertTrue(mock_collect.call_args.kwargs['probe_producer_pods'])


class MergeKcosLldpTests(SimpleTestCase):
    def test_kcos_connection_local_port_eaglefp(self):
        from connect.topology_lldp import kcos_connection_local_port

        row = {
            'slot': '1',
            'from': 'eaglefp0fo0',
            'to': '13.4',
            'link': 'UP',
        }
        self.assertEqual(kcos_connection_local_port(row), '1.0')
        row2 = {
            'card_number': 1,
            'from_interface': 'eaglefp2fo0',
            'to_switch_port': '13.6',
            'link_state': 'UP',
        }
        self.assertEqual(kcos_connection_local_port(row2), '1.2')

    def test_synthesize_b2b_lldp_from_serial_pairs(self):
        from connect.topology_lldp import synthesize_kcos_b2b_lldp_neighbors

        ports = [
            {
                'node_name': 'cn-a',
                'from_interface': 'eaglefp0fo0',
                'card_number': 1,
                'to_switch_port': '13.4',
                'link_state': 'UP',
                'transceiver_serial': 'BQ4Q13X22400382',
            },
            {
                'node_name': 'cn-a',
                'from_interface': 'eaglefp2fo0',
                'card_number': 1,
                'to_switch_port': '13.6',
                'link_state': 'UP',
                'transceiver_serial': 'BQ4Q13X22400382',
            },
        ]
        rows = synthesize_kcos_b2b_lldp_neighbors(
            ports, hostname='merpro2c', mgmt_ip='203.0.113.37',
        )
        self.assertEqual(len(rows), 2)
        by_local = {r['local_port']: r for r in rows}
        self.assertEqual(by_local['1.0']['remote_port'], '1.2')
        self.assertEqual(by_local['1.2']['remote_port'], '1.0')
        self.assertEqual(by_local['1.0']['remote_device'], 'merpro2c')
        self.assertTrue(by_local['1.0']['b2b'])

    def test_synthesize_b2b_does_not_clique_across_cards(self):
        from connect.topology_lldp import synthesize_kcos_b2b_lldp_neighbors

        ports = [
            {
                'node_name': 'cn-a',
                'from_interface': 'eaglefp1fo0',
                'card_number': 1,
                'link_state': 'UP',
                'transceiver_serial': 'SAME-SERIAL',
            },
            {
                'node_name': 'cn-a',
                'from_interface': 'eaglefp3fo0',
                'card_number': 1,
                'link_state': 'UP',
                'transceiver_serial': 'SAME-SERIAL',
            },
            {
                'node_name': 'cn-b',
                'from_interface': 'eaglefp1fo0',
                'card_number': 7,
                'link_state': 'UP',
                'transceiver_serial': 'SAME-SERIAL',
            },
            {
                'node_name': 'cn-b',
                'from_interface': 'eaglefp3fo0',
                'card_number': 7,
                'link_state': 'UP',
                'transceiver_serial': 'SAME-SERIAL',
            },
        ]
        rows = synthesize_kcos_b2b_lldp_neighbors(
            ports, hostname='merpro3n', mgmt_ip='198.18.1.254',
        )
        self.assertEqual(len(rows), 4)
        pairs = {(r['local_port'], r['remote_port']) for r in rows}
        self.assertIn(('1.1', '1.3'), pairs)
        self.assertIn(('7.1', '7.3'), pairs)
        self.assertNotIn(('1.1', '7.1'), pairs)

    def test_merge_bps_topology_rollup_lane_to_phys(self):
        from connect.topology_lldp import merge_lldp_into_bps_topology

        bps = {
            'slots': [{
                'id': 1,
                'physical_ports': [{
                    'id': 1,
                    'currentMode': '400G',
                    'lanes': [{'id': '1.0', 'link': 'up'}],
                    'total_lanes': 1,
                }],
            }],
        }
        neighbors = [{
            'local_port': '1.1',
            'remote_device': 'merpro3n',
            'remote_port': '1.3',
            'b2b': True,
        }]
        merge_lldp_into_bps_topology(bps, neighbors)
        pp = bps['slots'][0]['physical_ports'][0]
        self.assertTrue(pp['has_lldp'])
        self.assertEqual(pp['lldp'][0]['remote_port'], '1.3')

    def test_merge_lldp_into_cards_dot_notation(self):
        from connect.topology_lldp import merge_lldp_into_cards

        cards = [{'card_number': 3, 'ports': [{'card_number': 3, 'port_number': 1}]}]
        neighbors = [{
            'local_port': '3.1',
            'remote_device': 'sonic-leaf',
            'remote_port': 'Ethernet16',
            'mgmt_ip': '198.51.100.30',
        }]
        merge_lldp_into_cards(cards, neighbors)
        self.assertTrue(cards[0]['has_lldp'])
        self.assertEqual(len(cards[0]['ports'][0]['lldp']), 1)

    def test_merge_lldp_into_bps_topology_lane_id(self):
        from connect.topology_lldp import merge_lldp_into_bps_topology

        bps = {
            'slots': [{
                'id': 3,
                'physical_ports': [{
                    'id': 3,
                    'currentMode': '100G',
                    'lanes': [{'id': '3.0', 'link': 'up'}],
                    'total_lanes': 4,
                }],
            }],
        }
        neighbors = [{
            'local_port': '3.0',
            'remote_device': 'sonic-leaf',
            'remote_port': 'Ethernet16',
            'mgmt_ip': '198.51.100.30',
        }]
        merge_lldp_into_bps_topology(bps, neighbors)
        pp = bps['slots'][0]['physical_ports'][0]
        self.assertTrue(pp['has_lldp'])
        self.assertEqual(pp['lanes'][0]['lldp'][0]['remote_device'], 'sonic-leaf')

    @override_settings(
        KCOS_ROOT_SSH_PASSWORD='',
        KCOS_ROOT_SSH_KEY='',
        ARESONE_ROOT_SSH_PASSWORD='',
        ARESONE_ROOT_SSH_KEY='',
    )
    def test_get_lldp_ssh_without_credentials_errors_cleanly(self):
        driver = KCOSDriver(ip='198.18.1.233', username='u', password='p')
        res = driver.get_lldp_ssh()
        self.assertFalse(res.success)
        self.assertIn('not configured', res.error)
