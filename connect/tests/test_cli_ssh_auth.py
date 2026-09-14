"""SSH auth helper tests."""
from __future__ import annotations

import os
from unittest import mock

from django.test import SimpleTestCase


class SshAuthCidrTests(SimpleTestCase):
    def test_default_private_ranges_allowed(self):
        from connect.labvault_cli.ssh_auth import addr_allowed

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(addr_allowed("127.0.0.1"))
            self.assertTrue(addr_allowed("10.1.2.3"))
            self.assertTrue(addr_allowed("192.168.1.10"))
            self.assertFalse(addr_allowed("8.8.8.8"))

    def test_normalize_username(self):
        from connect.labvault_cli.ssh_auth import normalize_username

        self.assertEqual(normalize_username("  admin  "), "admin")
