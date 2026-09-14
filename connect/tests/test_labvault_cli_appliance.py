"""Unit tests for LabVault appliance CLI parser/runner/catalog."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase, RequestFactory


class ParserTests(SimpleTestCase):
    def test_longest_match_and_kwargs(self):
        from connect.labvault_cli import commands as _c  # noqa: F401
        from connect.labvault_cli.parser import parse_line

        cmd, args = parse_line('show devices limit=5')
        self.assertEqual(cmd, "show devices")
        self.assertEqual(args.get("limit"), 5)

    def test_quoted_reason(self):
        from connect.labvault_cli import commands as _c  # noqa: F401
        from connect.labvault_cli.parser import parse_line

        cmd, args = parse_line('service restart heartbeat reason="stale telemetry"')
        self.assertEqual(cmd, "service restart")
        self.assertEqual(args.get("name"), "heartbeat")
        self.assertEqual(args.get("reason"), "stale telemetry")

    def test_unknown_command(self):
        from connect.labvault_cli import commands as _c  # noqa: F401
        from connect.labvault_cli.parser import ParseError, parse_line

        with self.assertRaises(ParseError):
            parse_line("rm -rf /")


class CatalogTests(SimpleTestCase):
    def test_restart_all_source_guard(self):
        from connect.labvault_cli.service_catalog import restart_all_targets

        ssh = restart_all_targets(source="ssh")
        web = restart_all_targets(source="web")
        self.assertIn("web", ssh)
        self.assertNotIn("web", web)
        self.assertNotIn("opsd", ssh)
        self.assertNotIn("db", ssh)
        self.assertNotIn("metrics-db", ssh)
        self.assertNotIn("cli-ssh", ssh)
        self.assertEqual(ssh[0], "heartbeat")

    def test_capability_matrix(self):
        from connect.labvault_cli.service_catalog import SERVICES, capability_allows

        ok, _ = capability_allows(SERVICES["web"], "restart", source="ssh")
        self.assertTrue(ok)
        ok, _ = capability_allows(SERVICES["web"], "restart", source="web")
        self.assertFalse(ok)
        ok, _ = capability_allows(SERVICES["db"], "restart", source="ssh")
        self.assertFalse(ok)


class RunnerAuthTests(SimpleTestCase):
    def test_staff_required(self):
        from connect.labvault_cli import commands as _c  # noqa: F401
        from connect.labvault_cli.runner import CliContext, invoke

        ctx = CliContext(user=AnonymousUser(), source="web")
        result = invoke(ctx, line="whoami")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 403)

    def test_service_restart_requires_reason_and_nonce(self):
        from connect.labvault_cli import commands as _c  # noqa: F401
        from connect.labvault_cli.runner import CliContext, invoke

        user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_staff=True,
            is_superuser=True,
            pk=1,
            has_perm=lambda p: True,
        )
        ctx = CliContext(user=user, source="ssh", session_id="s1")
        result = invoke(ctx, line="service restart heartbeat")
        self.assertFalse(result.ok)
        self.assertIn(result.payload.get("error"), ("reason_required", "confirmation_required"))


class DeviceListTests(SimpleTestCase):
    def test_show_devices_uses_hostname(self):
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
                return list.__getitem__(self, item)

        with mock.patch("connect.models.Device") as Device:
            Device.objects.all.return_value = _QS([device])
            out = show_devices(req, limit=5)
        self.assertEqual(out["devices"][0]["name"], "sw1.lab")
        self.assertEqual(out["devices"][0]["vendor"], "arista")
