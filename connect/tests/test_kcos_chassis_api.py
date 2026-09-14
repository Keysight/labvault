"""Tests for KCOS /chassis/api/v2 (IxOS REST on KCOS) fallback — HTRex / T-Rex."""
from unittest.mock import patch

from django.test import SimpleTestCase

from connect.keysight_drivers.ixos import DriverResult
from connect.keysight_drivers.kcos import KCOSDriver


class KcosChassisApiTests(SimpleTestCase):
    def test_map_chassis_api_port(self):
        d = KCOSDriver('192.0.2.1')
        out = d._map_chassis_api_port_to_labvault({
            'cardNumber': 2,
            'portNumber': 4,
            'linkState': 'Up',
            'owner': 'Bob',
            'speed': '100000',
            'transceiverModel': 'QSFP-DD',
        }, 0)
        self.assertEqual(out['card_number'], 2)
        self.assertEqual(out['port_number'], 4)
        self.assertEqual(out['link_state'], 'up')
        self.assertEqual(out['owner'], 'Bob')
        self.assertIn('100', out['speed'])

    def test_ports_from_chassis_api_tries_ixos_prefix_after_404(self):
        d = KCOSDriver('192.0.2.1')
        calls = []

        def fake_get(subpath):
            calls.append(subpath)
            if subpath == '/ports':
                return DriverResult(error='HTTP 404')
            return DriverResult(success=True, data=[{
                'cardNumber': 1,
                'portNumber': 0,
                'linkState': 'down',
                'owner': '',
            }])

        with patch.object(d, '_get_chassis_api', side_effect=fake_get):
            ports = d._ports_from_chassis_rest_api()
        self.assertIsNotNone(ports)
        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0]['card_number'], 1)
        self.assertEqual(ports[0]['link_state'], 'down')
        self.assertIn('/ports', calls)
        self.assertIn('/ixos/ports', calls)

    def test_merge_num_ports_from_cards_endpoint(self):
        d = KCOSDriver('192.0.2.1')
        cards = [
            {'is_mgmt_slot': False, 'card_number': 1, 'num_ports': 0},
        ]

        def fake_get(subpath):
            if subpath == '/cards':
                return DriverResult(success=True, data=[
                    {'cardNumber': 1, 'numberOfPorts': 8},
                ])
            return DriverResult(error='HTTP 404')

        with patch.object(d, '_get_chassis_api', side_effect=fake_get):
            d._merge_num_ports_from_chassis_rest_api(cards)
        self.assertEqual(cards[0]['num_ports'], 8)

    def test_normalize_link_accepts_non_string(self):
        self.assertEqual(KCOSDriver._normalize_link('')[1], 'off')
        out = KCOSDriver._normalize_link(None)
        self.assertEqual(out[0], 'unknown')
