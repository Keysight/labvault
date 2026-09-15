"""First-boot credentials: random by default; demo values only when opted in."""
from __future__ import annotations

import os
from io import StringIO
from unittest import TestCase as UnitTestCase
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from connect.labvault_bootstrap_defaults import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_FLEET_TOKEN,
    bootstrap_password,
    fleet_token_value,
)
from connect.models import APIToken


class BootstrapDefaultsUnitTests(UnitTestCase):
    def test_random_by_default(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "LABVAULT_BOOTSTRAP_RANDOM",
                "LABVAULT_BOOTSTRAP_PASSWORD",
                "LABVAULT_DEMO_ADMIN_PASSWORD",
                "LABVAULT_FLEET_TOKEN",
                "LABVAULT_DEMO_API_TOKEN",
                "LABVAULT_DEMO_DEFAULTS",
            }
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(DEFAULT_ADMIN_PASSWORD, "labvault!")
            pw = bootstrap_password()
            tok = fleet_token_value()
            self.assertNotEqual(pw, DEFAULT_ADMIN_PASSWORD)
            self.assertNotEqual(tok, DEFAULT_FLEET_TOKEN)
            self.assertGreaterEqual(len(pw), 16)

    def test_demo_defaults_opt_in(self):
        with patch.dict(os.environ, {"LABVAULT_DEMO_DEFAULTS": "1", "LABVAULT_BOOTSTRAP_RANDOM": ""}, clear=False):
            self.assertEqual(bootstrap_password(), DEFAULT_ADMIN_PASSWORD)
            self.assertEqual(fleet_token_value(), DEFAULT_FLEET_TOKEN)

    def test_env_overrides(self):
        with patch.dict(
            os.environ,
            {
                "LABVAULT_BOOTSTRAP_PASSWORD": "OtherPass1!",
                "LABVAULT_DEMO_API_TOKEN": "other-token",
                "LABVAULT_BOOTSTRAP_RANDOM": "",
                "LABVAULT_DEMO_ADMIN_PASSWORD": "",
                "LABVAULT_FLEET_TOKEN": "",
                "LABVAULT_DEMO_DEFAULTS": "",
            },
            clear=False,
        ):
            self.assertEqual(bootstrap_password(), "OtherPass1!")
            self.assertEqual(fleet_token_value(), "other-token")


class BootstrapLabvaultCommandTests(TestCase):
    def test_seeds_admin_and_demo_api_token_when_opted_in(self):
        out = StringIO()
        with patch.dict(os.environ, {"LABVAULT_DEMO_DEFAULTS": "1", "LABVAULT_BOOTSTRAP_RANDOM": ""}):
            call_command("bootstrap_labvault", stdout=out)
        admin = get_user_model().objects.get(username="admin")
        self.assertTrue(admin.check_password("labvault!"))
        tok = APIToken.objects.get(user=admin, name="demo-api")
        self.assertEqual(tok.token, "labvault-default-api-token")
        self.assertTrue(tok.enabled)

    def test_unwritable_token_file_does_not_block_restore(self):
        from pathlib import Path

        out = StringIO()
        orig = Path.write_text

        def boom(self, *args, **kwargs):
            if str(self).endswith("blocked-token"):
                raise PermissionError("denied")
            return orig(self, *args, **kwargs)

        empty = Path(__file__).resolve().parents[2] / "resources" / "examples" / "empty-labvault-export.json"
        with patch.object(Path, "write_text", boom):
            with patch.dict(os.environ, {"LABVAULT_DEMO_DEFAULTS": "1", "LABVAULT_BOOTSTRAP_RANDOM": ""}):
                call_command(
                    "bootstrap_labvault",
                    "--live",
                    "--restore",
                    str(empty),
                    "--token-file",
                    "/tmp/blocked-token",
                    stdout=out,
                )
        text = out.getvalue()
        self.assertIn("warning skip_write", text)
        self.assertIn("restore_ok", text)
        self.assertTrue(get_user_model().objects.filter(username="admin").exists())

    def test_second_run_does_not_reset_existing_password_or_token(self):
        User = get_user_model()
        admin = User.objects.create_superuser("admin", "admin@localhost", "old-random")
        APIToken.objects.create(user=admin, name="demo-api", token="old-random-token-value", enabled=True)
        with patch.dict(os.environ, {"LABVAULT_DEMO_DEFAULTS": "1", "LABVAULT_BOOTSTRAP_RANDOM": ""}):
            call_command("bootstrap_labvault")
        admin.refresh_from_db()
        self.assertTrue(admin.check_password("old-random"))
        tok = APIToken.objects.get(user=admin, name="demo-api")
        self.assertEqual(tok.token, "old-random-token-value")
