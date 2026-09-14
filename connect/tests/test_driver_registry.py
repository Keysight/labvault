"""Tests for driver plugin registry."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from connect.driver_registry import discover_plugin_manifests, plugin_mode


class DriverRegistryTests(SimpleTestCase):
    def test_plugin_mode_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(plugin_mode(), 'off')

    def test_discover_sample_manifest(self):
        sample = Path(__file__).resolve().parents[2] / 'deploy/google-demo/sample-drivers'
        if not sample.is_dir():
            self.skipTest('sample drivers not present')
        with mock.patch.dict(os.environ, {
            'LABVAULT_DRIVER_PATH': str(sample),
            'LABVAULT_DRIVER_PLUGIN_MODE': 'dropin',
        }, clear=False):
            discover_plugin_manifests(force=True)
            manifests = discover_plugin_manifests()
        self.assertIn('demo_vendor', manifests)
        self.assertEqual(manifests['demo_vendor'].class_name, 'DemoVendorDriver')
