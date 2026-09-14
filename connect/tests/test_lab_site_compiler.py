"""Tests for LabVault Site v1 compiler."""
from django.test import SimpleTestCase

from connect.lab_site_compiler import compile_site_v1


class LabSiteCompilerTests(SimpleTestCase):
    def test_compile_minimal_site_v1(self):
        doc = {
            'version': 1,
            'name': 'Test-Lab',
            'site_tags': ['demo'],
            'ocs': {'ip': '10.0.0.1', 'user': 'admin'},
            'switches': [{'ip': '10.0.0.2', 'role': 'arista', 'mapping': 'site'}],
            'chassis': [{'ip': '10.0.0.3', 'type': 'aresone'}],
        }
        site = compile_site_v1(doc)
        self.assertEqual(site['version'], 3)
        self.assertEqual(site['ocs_controller']['ip'], '10.0.0.1')
        self.assertEqual(len(site['arista_switches']), 1)
        self.assertEqual(len(site['ares_switches']), 1)
