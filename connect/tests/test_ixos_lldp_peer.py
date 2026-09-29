"""Tests for IxOS ``show lldp-peer-info data`` parsing (XGS12 Card/Port format)."""
from django.test import SimpleTestCase

from connect.keysight_drivers.ixos import IxOSDriver


SAMPLE_XGS12 = """
Chassis peer info
No chassis LLDP neighbor info available.
Card 1 Port 1
        System MAC: 00:1A:C5:01:18:7C
        Port ID: 1/2
        System name: xgs12-bpsst
        System IP: 198.18.2.160
        Port description: 100000 Mbps
Card 1 Port 2
        System MAC: 00:1A:C5:01:18:7C
        Port ID: 1/1
        System name: xgs12-bpsst
        System IP: 198.18.2.160
        Port description: 100000 Mbps
Card 3 Port 1
        System MAC: 00:1A:C5:01:18:7C
        Port ID: 3/2
        System name: xgs12-bpsst
        System IP: 198.18.2.160
        Port description: 100000 Mbps
"""


class IxOSLldpPeerParseTests(SimpleTestCase):
    def test_card_port_format(self):
        rows = IxOSDriver._parse_lldp_peer_info(SAMPLE_XGS12)
        self.assertEqual(len(rows), 3)
        by_local = {r['local_port']: r for r in rows}
        self.assertIn('1/1', by_local)
        self.assertEqual(by_local['1/1']['remote_port'], '1/2')
        self.assertEqual(by_local['1/1']['remote_device'], 'xgs12-bpsst')
        self.assertEqual(by_local['1/1']['mgmt_ip'], '198.18.2.160')
        self.assertEqual(by_local['3/1']['remote_port'], '3/2')

    def test_port_dot_format_still_works(self):
        raw = """
Port 1.1
        System name: sonic
        System IP: 198.18.3.95
        Port ID: Eth57/1
"""
        rows = IxOSDriver._parse_lldp_peer_info(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['local_port'], '1.1')
        self.assertEqual(rows[0]['remote_device'], 'sonic')

    def test_disabled_status_detected(self):
        raw = (
            'Chassis peer info\n'
            'No chassis LLDP neighbor info available.\n'
            'lldp-peer-info status is disabled, please enable it to get neighbor info.\n'
        )
        self.assertTrue(IxOSDriver._lldp_peer_info_disabled(raw))
        self.assertTrue(
            IxOSDriver._lldp_peer_info_disabled('lldp-peer-info status: disabled for all ports.')
        )
        self.assertFalse(IxOSDriver._lldp_peer_info_disabled(SAMPLE_XGS12))
