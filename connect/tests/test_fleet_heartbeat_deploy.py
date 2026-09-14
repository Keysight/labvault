"""Prevention tests for fleet heartbeat deploy pitfalls."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase


class HeartbeatIntervalDefaultsTests(SimpleTestCase):
    def test_default_interval_is_fleet_safe(self):
        from connect.fleet_heartbeat import heartbeat_interval_seconds

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LABVAULT_HEARTBEAT_INTERVAL_SECONDS', None)
            self.assertEqual(heartbeat_interval_seconds(), 120)

    def test_interval_env_honored_with_floor(self):
        from connect.fleet_heartbeat import heartbeat_interval_seconds

        with mock.patch.dict(os.environ, {'LABVAULT_HEARTBEAT_INTERVAL_SECONDS': '30'}):
            self.assertEqual(heartbeat_interval_seconds(), 30)
        with mock.patch.dict(os.environ, {'LABVAULT_HEARTBEAT_INTERVAL_SECONDS': '5'}):
            self.assertEqual(heartbeat_interval_seconds(), 15)


class HeartbeatLockPathTests(SimpleTestCase):
    def test_prefers_writable_cache_dir_over_root_tmp(self):
        from connect.fleet_heartbeat import _heartbeat_lock_path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'django_cache'
            cache.mkdir()
            preferred = str(cache / 'fleet_heartbeat.lock')
            rootish = '/tmp/labvault_fleet_heartbeat.lock'
            with mock.patch.dict(
                os.environ,
                {
                    'LABVAULT_HEARTBEAT_LOCK': preferred,
                },
                clear=False,
            ):
                # Make /tmp candidate fail creatability by pointing configured first.
                path = _heartbeat_lock_path()
            self.assertEqual(path, preferred)
            self.assertTrue(Path(path).exists())
            self.assertNotEqual(path, rootish)
