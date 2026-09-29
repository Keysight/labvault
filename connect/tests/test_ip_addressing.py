"""Unit tests for dual-stack management addressing."""
from django.test import SimpleTestCase

from connect.ip_addressing import (
    bracket_host,
    derive_dhcpv6_ocs_lab,
    display_addresses,
    display_mgmt_address,
    is_fqdn_hostname,
    is_link_local,
    mgmt_ip_bundle,
    normalize_ip,
    normalize_preferred,
    parse_address_input,
    resolve_connect_address,
    resolve_connect_targets,
    resolve_mgmt_ipv6_for_connect,
    unbracket_host,
)
from connect.models import Device, KeysightChassis


class IpAddressingTests(SimpleTestCase):
    def test_normalize_ipv6(self):
        self.assertEqual(normalize_ip('2001:db8::1'), '2001:db8::1')
        self.assertEqual(normalize_ip('[fe80::1]'), 'fe80::1')

    def test_bracket_host(self):
        self.assertEqual(bracket_host('2001:db8::1'), '[2001:db8::1]')
        self.assertEqual(bracket_host('10.0.0.1'), '10.0.0.1')
        self.assertEqual(unbracket_host('[fe80::1]'), 'fe80::1')

    def test_resolve_auto_ipv4_first(self):
        self.assertEqual(
            resolve_connect_address(ipv4='10.1.1.1', ipv6='fe80::1', preferred='auto'),
            '10.1.1.1',
        )

    def test_resolve_prefer_ipv6(self):
        self.assertEqual(
            resolve_connect_address(ipv4='10.1.1.1', ipv6='2001:db8::5', preferred='ipv6'),
            '2001:db8::5',
        )

    def test_resolve_ipv6_only(self):
        self.assertEqual(
            resolve_connect_address(ipv4='', ipv6='fe80::dead:beef', preferred='auto'),
            'fe80::dead:beef',
        )

    def test_link_local_slaac(self):
        self.assertTrue(is_link_local('fe80::1'))

    def test_parse_dual_input(self):
        v4, v6 = parse_address_input('10.0.0.1, fe80::1')
        self.assertEqual(v4, '10.0.0.1')
        self.assertEqual(v6, 'fe80::1')

    def test_display_addresses(self):
        s = display_addresses(ipv4='10.0.0.1', ipv6='fe80::1')
        self.assertIn('v4', s)
        self.assertIn('SLAAC', s)

    def test_derive_dhcpv6_ocs_lab(self):
        self.assertEqual(
            derive_dhcpv6_ocs_lab(
                '192.0.2.21',
                prefix='2001:db8:5',
                ipv4_subnet=('192', '0', '2'),
            ),
            '2001:db8:5:221',
        )
        self.assertEqual(derive_dhcpv6_ocs_lab('10.0.0.1'), '')
        self.assertEqual(derive_dhcpv6_ocs_lab('192.0.2.21'), '')

    def test_fqdn_hostname_first(self):
        self.assertTrue(is_fqdn_hostname('chassis1.example.com'))
        self.assertFalse(is_fqdn_hostname('Aresone_1'))
        self.assertEqual(
            resolve_connect_targets(
                ipv4='192.0.2.31',
                ipv6='',
                hostname='ares1.example.com',
                preferred='ipv4',
            ),
            ['ares1.example.com', '192.0.2.31'],
        )

    def test_derived_dhcpv6_not_used_for_connect(self):
        self.assertEqual(
            resolve_mgmt_ipv6_for_connect(
                ipv4='192.0.2.31',
                mgmt_ipv6='',
                ipv6_source='dhcpv6',
            ),
            '',
        )
        self.assertEqual(
            resolve_connect_address(
                ipv4='192.0.2.31',
                ipv6=resolve_mgmt_ipv6_for_connect(
                    ipv4='192.0.2.31', mgmt_ipv6='', ipv6_source='dhcpv6',
                ),
                preferred='ipv6',
                hostname='',
            ),
            '192.0.2.31',
        )

    def test_dual_targets_ipv4_then_ipv6(self):
        self.assertEqual(
            resolve_connect_targets(
                ipv4='192.0.2.39',
                ipv6='2001:db8::39',
                preferred='dual',
            ),
            ['192.0.2.39', '2001:db8::39'],
        )

    def test_dual_connect_address_stays_ipv4_primary(self):
        self.assertEqual(
            resolve_connect_address(
                ipv4='192.0.2.39',
                ipv6='2001:db8::39',
                preferred='dual',
            ),
            '192.0.2.39',
        )

    def test_dual_ipv4_only_single_target(self):
        self.assertEqual(
            resolve_connect_targets(ipv4='10.1.1.1', ipv6='', preferred='dual'),
            ['10.1.1.1'],
        )


class ModelConnectAddressTests(SimpleTestCase):
    def test_normalize_legacy_auto(self):
        self.assertEqual(normalize_preferred('auto'), 'ipv4')

    def test_dual_display_both(self):
        self.assertEqual(
            display_mgmt_address(
                ipv4='192.0.2.21',
                ipv6='2001:db8::5:8421',
                preferred='dual',
            ),
            '2001:db8::5:8421 (192.0.2.21)',
        )

    def test_mgmt_ip_bundle(self):
        b = mgmt_ip_bundle(
            ipv4='192.0.2.39',
            ipv6='2001:db8::5:8439',
            preferred='ipv6',
        )
        self.assertEqual(b['mgmt_display'], '2001:db8::5:8439')
        self.assertEqual(b['device_ip'], '192.0.2.39')

    def test_device_defaults_ipv4_only(self):
        d = Device(ip_address='198.51.100.1', username='u', password='p')
        self.assertEqual(d.connect_address, '198.51.100.1')
        self.assertEqual(d.preferred_ip_version, 'ipv4')

    def test_device_prefer_ipv6(self):
        d = Device(
            ip_address='198.51.100.1',
            mgmt_ipv6='2001:db8::10',
            mgmt_ipv6_source='static',
            preferred_ip_version='ipv6',
            username='u',
            password='p',
        )
        self.assertEqual(d.connect_address, '2001:db8::10')

    def test_chassis_connect_address(self):
        ch = KeysightChassis(
            ip_address='198.18.2.160',
            mgmt_ipv6='',
            username='admin',
            password='admin',
        )
        self.assertEqual(ch.connect_address, '198.18.2.160')

    def test_display_mgmt_prefer_ipv6(self):
        self.assertEqual(
            display_mgmt_address(
                ipv4='192.0.2.21',
                ipv6='2001:db8::21',
                preferred='ipv6',
            ),
            '2001:db8::21',
        )

    def test_device_dual_stack_targets(self):
        d = Device(
            ip_address='192.0.2.21',
            mgmt_ipv6='fe80::21',
            mgmt_ipv6_source='slaac',
            preferred_ip_version='dual',
            username='u',
            password='p',
        )
        self.assertEqual(d.connect_targets, ['192.0.2.21', 'fe80::21'])
        self.assertEqual(d.connect_address, '192.0.2.21')

    def test_chassis_dhcpv6_display_only(self):
        ch = KeysightChassis(
            ip_address='192.0.2.31',
            mgmt_ipv6='',
            mgmt_ipv6_source='dhcpv6',
            preferred_ip_version='ipv6',
            username='admin',
            password='admin',
        )
        self.assertEqual(ch.connect_address, '192.0.2.31')
        self.assertEqual(ch.effective_mgmt_ipv6, '')
