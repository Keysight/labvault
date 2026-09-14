"""Unit tests for PCPU / IxOS version helpers."""

import json

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from connect.lab_usage_insights import build_usage_insights
from connect.models import KeysightChassis
from connect.pcpu_versions import (
    merge_chassis_and_pcpu_versions,
    normalize_ixos_applications,
    parse_pcpu_ixos_blob,
    version_fields_from_chassis,
)
from connect.tests.timeline_test_helpers import create_chassis_node, create_topology
from datetime import timedelta
from unittest.mock import patch


class PcpuVersionParserTests(SimpleTestCase):
    def test_normalize_ixos_applications_list(self):
        raw = [
            {'name': 'IxOS', 'version': '9.30.2201.7'},
            {'name': 'IxNetwork API Server', 'version': '9.30.2201.7'},
            {'name': 'IxServer', 'version': '1.2.3'},
        ]
        out = normalize_ixos_applications(raw)
        self.assertEqual(out['ixos_version'], '9.30.2201.7')
        self.assertEqual(out['ixnetwork_version'], '9.30.2201.7')
        self.assertEqual(out['applications']['IxServer'], '1.2.3')

    def test_parse_pcpu_ixos_blob(self):
        payload = {
            'ixosApplications': [
                {'name': 'IxOS', 'version': '9.10.1'},
                {'name': 'IxNetwork', 'version': '9.10.2'},
            ],
        }
        blob = f'---LV_IXOS---\n{json.dumps(payload)}'
        out = parse_pcpu_ixos_blob(blob)
        self.assertEqual(out['ixos_version'], '9.10.1')
        self.assertEqual(out['ixnetwork_version'], '9.10.2')


class PcpuVersionInsightsTests(TestCase):
    databases = {'default', 'np_timeseries'}

    def setUp(self):
        self.topo = create_topology('pcpu-ver-test')
        self.node = create_chassis_node(self.topo)
        ch = KeysightChassis.objects.get(pk=self.node.extra['chassis_id'])
        ch.ixos_version = '9.00.0'
        ch.ixos_applications = json.dumps({
            'IxOS': '9.00.0',
            'IxNetwork': '9.00.1',
            'IxServer': '9.00.2',
        })
        ch.save()
        now = timezone.now()
        from connect.models import LabMetricSample

        LabMetricSample.objects.using('np_timeseries').bulk_create([
            LabMetricSample(
                topology_id=self.topo.pk, resource_key=f'node_{self.node.pk}__1.1',
                metric='cpu_pct', value=40.0, sampled_at=now,
            ),
            LabMetricSample(
                topology_id=self.topo.pk, resource_key=f'node_{self.node.pk}__1.1',
                metric='mem_pct', value=30.0, sampled_at=now,
            ),
        ])

    def test_version_fields_from_chassis(self):
        ch = KeysightChassis.objects.get(pk=self.node.extra['chassis_id'])
        out = version_fields_from_chassis(ch)
        self.assertEqual(out['ixos_version'], '9.00.0')
        self.assertEqual(out['ixnetwork_version'], '9.00.1')

    def test_chassis_versions_override_pcpu_ssh(self):
        chassis_apps = {
            'node_1': {
                'ixos_version': '26.4.2600.24',
                'ixnetwork_version': '26.4.4100.4',
                'applications': {
                    'IxOS': '26.4.2600.24',
                    'IxNetwork': '26.4.4100.4',
                    'IxServer': '26.4.1.0',
                },
            },
        }
        pcpu_apps = {
            '10.0.1.1': {
                'ixos_version': '9.30.1',
                'ixnetwork_version': '9.30.2',
                'applications': {
                    'IxOS': '9.30.1',
                    'IxNetwork': '9.30.2',
                    'IxServer': '9.30.3',
                },
            },
        }
        out = merge_chassis_and_pcpu_versions(
            parent='node_1',
            mgmt_ip='10.0.1.1',
            pcpu_apps=pcpu_apps,
            chassis_apps_by_parent=chassis_apps,
        )
        self.assertEqual(out['ixos_version'], '26.4.2600.24')
        self.assertEqual(out['ixnetwork_version'], '26.4.4100.4')
        self.assertEqual(out['applications']['IxServer'], '9.30.3')

    @patch('connect.lab_usage_insights.build_enriched_resource_catalog')
    @patch('connect.lab_usage_insights._load_fabric_light')
    def test_pcpu_fleet_includes_app_versions(self, mock_fabric, mock_catalog):
        mock_fabric.return_value = {'devices': [], 'port_links': [], 'port_nodes': []}
        mock_catalog.return_value = {
            f'node_{self.node.pk}': {
                'profile': 'keysight_chassis',
                'label': 'Test Chassis',
                'metrics': ['cpu_pct', 'mem_pct'],
                'node_id': self.node.pk,
                'node_type': 'chassis',
                'mgmt_ip': '10.1.1.1',
            },
            f'node_{self.node.pk}__1.1': {
                'profile': 'keysight_port',
                'label': '1.1',
                'parent': f'node_{self.node.pk}',
                'metrics': ['cpu_pct', 'mem_pct', 'port_ownership', 'bps_in', 'bps_out'],
                'node_id': self.node.pk,
                'pcpu_ip': '10.0.1.1',
                'chassis_ip': '10.1.1.1',
            },
        }
        cache.set(f'pcpu_apps:{self.topo.pk}', {
            '10.0.1.1': {
                'ixos_version': '9.30.1',
                'ixnetwork_version': '9.30.2',
                'applications': {
                    'IxOS': '9.30.1',
                    'IxNetwork': '9.30.2',
                    'IxServer': '9.30.3',
                },
            },
        }, 600)
        now = timezone.now()
        payload = build_usage_insights(self.topo.pk, now - timedelta(hours=1), now)
        fleet = payload['pcpu_fleet']
        self.assertEqual(len(fleet), 1)
        row = fleet[0]
        self.assertEqual(row['mgmt_ip'], '10.0.1.1')
        self.assertEqual(row['ixos_version'], '9.00.0')
        self.assertEqual(row['ixnetwork_version'], '9.00.1')
        self.assertEqual(row['applications']['IxServer'], '9.00.2')
        devices = payload['devices']
        chassis_dev = next(d for d in devices if d['resource_key'] == f'node_{self.node.pk}')
        self.assertEqual(chassis_dev['ixos_version'], '9.00.0')
        self.assertEqual(chassis_dev['ixnetwork_version'], '9.00.1')
