"""Tests for diagnostics payload and log store."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase

from connect.diagnostics import build_diagnostics_bundle_files, build_diagnostics_payload
from connect.diagnostics_log_store import read_host_logs


class DiagnosticsLogStoreTests(SimpleTestCase):
    def test_read_host_logs_empty_when_unconfigured(self):
        with mock.patch.dict(os.environ, {'LABVAULT_DIAGNOSTICS_LOG_DIR': ''}, clear=False):
            out = read_host_logs(limit=10)
        self.assertFalse(out['configured'])

    def test_read_host_logs_from_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            (log_dir / 'web.jsonl').write_text(
                json.dumps({'ts': '2026-01-01T00:00:00Z', 'level': 'ERROR', 'message': 'boom'}) + '\n',
                encoding='utf-8',
            )
            with mock.patch.dict(os.environ, {'LABVAULT_DIAGNOSTICS_LOG_DIR': tmp}, clear=False):
                out = read_host_logs(limit=10, service='web')
            self.assertTrue(out['configured'])
            self.assertEqual(len(out['entries']), 1)
            self.assertEqual(out['entries'][0]['level'], 'ERROR')


class DiagnosticsPayloadTests(TestCase):
    def test_build_payload_has_checks_and_workers(self):
        payload = build_diagnostics_payload(include_logs=False, log_limit=50)
        self.assertIn('checks', payload)
        self.assertIn('summary', payload)
        self.assertIn('workers', payload['sections'])
        self.assertIsInstance(payload['checks'], list)

    def test_bundle_includes_host_logs_section(self):
        files = build_diagnostics_bundle_files(log_limit=50)
        self.assertIn('full_report.json', files)
        self.assertIn('sections/workers.json', files)
