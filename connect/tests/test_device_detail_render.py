"""Device detail must render for every vendor — missing URL names 500 the page."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from connect.models import Device


class DeviceDetailRenderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.c", "test")
        self.client.login(username="admin", password="test")
        self.device = Device.objects.create(
            ip_address="10.1.1.1",
            hostname="sw1",
            username="u",
            password="p",
            vendor_type="arista",
            status="offline",
        )

    def test_enable_lldp_url_wired(self):
        self.assertEqual(
            reverse("device_enable_lldp", args=[self.device.pk]),
            f"/device/{self.device.pk}/enable-lldp/",
        )

    def test_offline_switch_detail_renders(self):
        r = self.client.get(reverse("device_detail", args=[self.device.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Enable LLDP")
