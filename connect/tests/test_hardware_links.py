"""Tests for hardware vs LabVault management URLs."""

from unittest.mock import patch

from django.test import SimpleTestCase

from connect.hardware_links import (
    hardware_login_url,
    hardware_login_url_for_object,
    hardware_web_host,
    labvault_detail_path,
)


class HardwareWebHostTests(SimpleTestCase):
    def test_ipv4_no_ptr(self):
        self.assertEqual(hardware_web_host('10.36.84.19'), '10.36.84.19')

    @patch('connect.hardware_links.reverse_dns_hostname', return_value='chassis1.lbj.is.keysight.com')
    def test_ipv4_with_ptr(self, _mock_ptr):
        self.assertEqual(
            hardware_web_host('10.36.84.19'),
            'chassis1.lbj.is.keysight.com',
        )

    def test_explicit_fqdn(self):
        self.assertEqual(
            hardware_web_host('10.36.84.19', resolved_hostname='chassis1.lbj.is.keysight.com'),
            'chassis1.lbj.is.keysight.com',
        )

    def test_ipv6_brackets_in_url(self):
        self.assertEqual(
            hardware_login_url('2620:17b:3:c000::5:8419'),
            'https://[2620:17b:3:c000::5:8419]/',
        )

    def test_fqdn_https_url(self):
        self.assertEqual(
            hardware_login_url('10.36.84.19', resolved_hostname='chassis1.lbj.is.keysight.com'),
            'https://chassis1.lbj.is.keysight.com/',
        )

    def test_empty(self):
        self.assertEqual(hardware_login_url(''), '')


class HardwareLoginForObjectTests(SimpleTestCase):
    @patch('connect.hardware_links.reverse_dns_hostname', return_value='device.lab.example.com')
    def test_device_uses_ptr(self, _mock_ptr):
        from connect.models import Device

        d = Device(pk=1, ip_address='10.1.2.3', hostname='short-name')
        self.assertEqual(
            hardware_login_url_for_object(d),
            'https://device.lab.example.com/',
        )

    def test_dual_display_label_does_not_break_hardware_url(self):
        from connect.models import Device

        d = Device(
            pk=2,
            ip_address='10.36.84.19',
            mgmt_ipv6='2620:17b:3:c000::5:8419',
            preferred_ip_version='dual',
        )
        self.assertEqual(
            hardware_login_url_for_object(d),
            'https://10.36.84.19/',
        )


class LabvaultDetailPathTests(SimpleTestCase):
    def test_device_path(self):
        from connect.models import Device

        d = Device(pk=42)
        self.assertEqual(labvault_detail_path(d), '/device/42/')

    def test_chassis_path(self):
        from connect.models import KeysightChassis

        c = KeysightChassis(pk=7)
        self.assertEqual(labvault_detail_path(c), '/keysight/chassis/7/')
