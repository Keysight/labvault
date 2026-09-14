"""CLI command handler smoke tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.test import RequestFactory, SimpleTestCase


class ShowDevicesCommandTests(SimpleTestCase):
    def test_show_devices_uses_hostname_not_name(self):
        from connect.labvault_cli.commands import show_devices

        device = SimpleNamespace(
            id=1,
            hostname="sw1.lab",
            ip_address="10.0.0.1",
            vendor_type="arista",
        )
        req = RequestFactory().get("/")
        req.user = SimpleNamespace(is_staff=True)

        class _QS(list):
            def __getitem__(self, item):
                if isinstance(item, slice):
                    return list.__getitem__(self, item)
                return list.__getitem__(self, item)

        with mock.patch("connect.models.Device") as Device:
            Device.objects.all.return_value = _QS([device])
            out = show_devices(req, limit=5)
        self.assertEqual(out["devices"][0]["name"], "sw1.lab")
        self.assertEqual(out["devices"][0]["vendor"], "arista")
