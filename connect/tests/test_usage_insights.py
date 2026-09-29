"""Tests for Lab Pulse usage insights aggregation."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from connect.aresone_ssh import parse_free_m_mem_pct
from connect.lab_usage_insights import (
    _insight_from_buckets,
    _is_fabric_mapped_switch_label,
    _link_state_from_events,
    _resolve_port_link_up,
    _series_stats,
    build_usage_insights,
    compact_pulse_payload,
)
from connect.models import LabMetricSample
from connect.tests.timeline_test_helpers import (
    create_chassis_node,
    create_switch_node,
    create_topology,
)
from connect.topology_resource_catalog import (
    attach_mgmt_ips_to_catalog,
    infer_pcpu_mgmt_ip_from_label,
    invalidate_resource_catalog_cache,
    load_port_pcpu_mgmt_index,
)


class UsageInsightsTests(TestCase):
    databases = {'default', 'np_timeseries'}

    def setUp(self):
        self.topo = create_topology('insights-test')
        self.node = create_chassis_node(self.topo)
        now = timezone.now()

        samples = [
            LabMetricSample(
                topology_id=self.topo.pk, resource_key=f'node_{self.node.pk}',
                metric='cpu_pct', value=10.0, sampled_at=now,
            ),
            LabMetricSample(
                topology_id=self.topo.pk, resource_key=f'node_{self.node.pk}__1.1',
                metric='port_ownership', value=1.0, sampled_at=now,
            ),
            LabMetricSample(
                topology_id=self.topo.pk, resource_key=f'node_{self.node.pk}__1.1',
                metric='cpu_pct', value=88.0, sampled_at=now,
            ),
            LabMetricSample(
                topology_id=self.topo.pk, resource_key=f'node_{self.node.pk}__1.1',
                metric='mem_pct', value=55.0, sampled_at=now,
            ),
            LabMetricSample(
                topology_id=self.topo.pk, resource_key=f'node_{self.node.pk}__1.2',
                metric='port_ownership', value=1.0, sampled_at=now,
            ),
            LabMetricSample(
                topology_id=self.topo.pk, resource_key=f'node_{self.node.pk}__1.2',
                metric='cpu_pct', value=88.0, sampled_at=now,
            ),
            LabMetricSample(
                topology_id=self.topo.pk, resource_key=f'node_{self.node.pk}__1.2',
                metric='mem_pct', value=55.0, sampled_at=now,
            ),
        ]
        LabMetricSample.objects.using('np_timeseries').bulk_create(samples)

    @patch('connect.lab_usage_insights.build_enriched_resource_catalog')
    @patch('connect.lab_usage_insights._load_fabric_light')
    def test_build_usage_insights_summary(self, mock_fabric, mock_catalog):
        mock_fabric.return_value = {'devices': [], 'port_links': [], 'port_nodes': []}
        mock_catalog.return_value = {
            f'node_{self.node.pk}': {
                'profile': 'keysight_chassis',
                'label': 'Test Chassis',
                'metrics': ['cpu_pct', 'mem_pct'],
                'node_id': self.node.pk,
                'node_type': 'chassis',
            },
            f'node_{self.node.pk}__1.1': {
                'profile': 'keysight_port',
                'label': '1.1',
                'metrics': ['cpu_pct', 'mem_pct', 'port_ownership', 'bps_in', 'bps_out'],
                'node_id': self.node.pk,
                'pcpu_ip': '10.0.0.1',
            },
            f'node_{self.node.pk}__1.2': {
                'profile': 'keysight_port',
                'label': '1.2',
                'metrics': ['cpu_pct', 'mem_pct', 'port_ownership', 'bps_in', 'bps_out'],
                'node_id': self.node.pk,
                'pcpu_ip': '10.0.0.1',
            },
        }
        now = timezone.now()
        payload = build_usage_insights(self.topo.pk, now - timedelta(hours=1), now)
        self.assertEqual(payload['topology_id'], self.topo.pk)
        self.assertGreaterEqual(payload['summary']['ports_owned'], 2)
        self.assertGreaterEqual(len(payload['waste_signals']), 2)
        self.assertGreaterEqual(len(payload['pcpu_fleet']), 1)
        self.assertIn('cpu_pct', payload['rankings'])
        self.assertEqual(payload['schema_version'], 2)
        self.assertIn('fleet', payload['time_series'])
        self.assertIn('nodes', payload['stress_graph'])
        self.assertIsInstance(payload['bottlenecks'], list)

    def test_parse_free_m_mem_pct(self):
        out = parse_free_m_mem_pct('Mem: 4096 1024 3072')
        self.assertAlmostEqual(out, 25.0)

    def test_series_stats_bps_uses_last_nonzero_when_latest_zero(self):
        series = [
            ['2026-06-15T10:00:00Z', 500.0],
            ['2026-06-15T10:05:00Z', 0.0],
        ]
        stats = _series_stats(series, metric='bps_in')
        self.assertEqual(stats['latest'], 500.0)

    def test_infer_pcpu_mgmt_ip_from_label(self):
        self.assertEqual(infer_pcpu_mgmt_ip_from_label('1.1'), '10.0.1.1')
        self.assertEqual(infer_pcpu_mgmt_ip_from_label('1.2.1'), '')
        self.assertEqual(infer_pcpu_mgmt_ip_from_label('Ethernet1'), '')
        self.assertEqual(infer_pcpu_mgmt_ip_from_label('1.3'), '10.0.1.3')
        self.assertEqual(infer_pcpu_mgmt_ip_from_label('1.3', chassis_type='aresone'), '10.0.1.1')
        self.assertEqual(infer_pcpu_mgmt_ip_from_label('3.1', chassis_type='AresONE-M'), '10.0.1.3')

    def test_chassis_port_metric_label_aresone_rg(self):
        from connect.topology_resource_catalog import chassis_port_metric_label, pcpu_mgmt_ip_from_port

        port = {
            'card_number': 1,
            'port_number': 11,
            'port_display': '2.1',
            'management_ip': '10.0.1.11',
        }
        self.assertEqual(chassis_port_metric_label(port), '1.2.1')
        self.assertEqual(pcpu_mgmt_ip_from_port(port), '10.0.1.11')

    def test_parse_port_bitrate_bytes_to_bps(self):
        from connect.metric_collectors import _parse_port_bitrate

        rx, tx = _parse_port_bitrate({'rxBytesRate': 1000, 'txBytesRate': 500})
        self.assertAlmostEqual(rx, 8000.0)
        self.assertAlmostEqual(tx, 4000.0)

    @patch('connect.lab_usage_insights.build_enriched_resource_catalog')
    @patch('connect.lab_usage_insights._load_fabric_light')
    def test_pcpu_fleet_infers_mgmt_from_port_label(self, mock_fabric, mock_catalog):
        """PCPU fleet groups ports when mgmt IP can be inferred from card.port labels."""
        mock_fabric.return_value = {'devices': [], 'port_links': [], 'port_nodes': []}
        mock_catalog.return_value = {
            f'node_{self.node.pk}': {
                'profile': 'keysight_chassis',
                'label': 'Test Chassis',
                'metrics': ['cpu_pct', 'mem_pct'],
                'node_id': self.node.pk,
                'node_type': 'chassis',
                'mgmt_ip': '192.0.2.35',
            },
            f'node_{self.node.pk}__1.1': {
                'profile': 'keysight_port',
                'label': '1.1',
                'parent': f'node_{self.node.pk}',
                'metrics': ['cpu_pct', 'mem_pct', 'port_ownership', 'bps_in', 'bps_out'],
                'node_id': self.node.pk,
                'chassis_ip': '192.0.2.35',
            },
            f'node_{self.node.pk}__1.2': {
                'profile': 'keysight_port',
                'label': '1.2',
                'parent': f'node_{self.node.pk}',
                'metrics': ['cpu_pct', 'mem_pct', 'port_ownership', 'bps_in', 'bps_out'],
                'node_id': self.node.pk,
                'chassis_ip': '192.0.2.35',
            },
        }
        now = timezone.now()
        payload = build_usage_insights(self.topo.pk, now - timedelta(hours=1), now)
        self.assertGreaterEqual(len(payload['pcpu_fleet']), 1)
        mgmt_ips = {p['mgmt_ip'] for p in payload['pcpu_fleet']}
        self.assertIn('10.0.1.1', mgmt_ips)
        self.assertIn('10.0.1.2', mgmt_ips)

    @patch('connect.lab_usage_insights.build_enriched_resource_catalog')
    @patch('connect.lab_usage_insights._load_fabric_light')
    def test_aresone_rg_keeps_card1_ports_on_rg1_pcpu(self, mock_fabric, mock_catalog):
        """AresONE 1.3 belongs on RG01 (10.0.1.1), not card.port 10.0.1.3."""
        mock_fabric.return_value = {'devices': [], 'port_links': [], 'port_nodes': []}
        parent = f'node_{self.node.pk}'
        mock_catalog.return_value = {
            parent: {
                'profile': 'keysight_chassis',
                'label': 'AresONE-M 07',
                'metrics': ['cpu_pct', 'mem_pct'],
                'node_id': self.node.pk,
                'node_type': 'chassis',
                'mgmt_ip': '192.0.2.37',
                'chassis_type': 'aresone',
            },
            f'{parent}__1.1': {
                'profile': 'keysight_port', 'label': '1.1', 'parent': parent,
                'metrics': ['cpu_pct', 'mem_pct', 'port_ownership', 'bps_in', 'bps_out'],
                'node_id': self.node.pk, 'chassis_ip': '192.0.2.37',
                'pcpu_ip': '10.0.1.1',
            },
            f'{parent}__1.3': {
                'profile': 'keysight_port', 'label': '1.3', 'parent': parent,
                'metrics': ['cpu_pct', 'mem_pct', 'port_ownership', 'bps_in', 'bps_out'],
                'node_id': self.node.pk, 'chassis_ip': '192.0.2.37',
                'pcpu_ip': '10.0.1.3',
            },
            f'{parent}__3.1': {
                'profile': 'keysight_port', 'label': '3.1', 'parent': parent,
                'metrics': ['cpu_pct', 'mem_pct', 'port_ownership', 'bps_in', 'bps_out'],
                'node_id': self.node.pk, 'chassis_ip': '192.0.2.37',
                'pcpu_ip': '10.0.1.3',
            },
        }
        now = timezone.now()
        LabMetricSample.objects.using('np_timeseries').bulk_create([
            LabMetricSample(
                topology_id=self.topo.pk, resource_key=f'{parent}__1.3',
                metric='cpu_pct', value=12.0, sampled_at=now,
            ),
            LabMetricSample(
                topology_id=self.topo.pk, resource_key=f'{parent}__3.1',
                metric='cpu_pct', value=12.0, sampled_at=now,
            ),
        ])
        payload = build_usage_insights(self.topo.pk, now - timedelta(hours=1), now)
        by_ip = {
            rg['mgmt_ip']: [p['label'] for p in (rg.get('ports') or [])]
            for rg in payload['pcpu_fleet']
        }
        self.assertIn('1.1', by_ip.get('10.0.1.1', []))
        self.assertIn('1.3', by_ip.get('10.0.1.1', []))
        self.assertIn('3.1', by_ip.get('10.0.1.3', []))
        self.assertNotIn('1.3', by_ip.get('10.0.1.3', []))

    def test_pcpu_ports_mark_fresh_vs_stale_samples(self):
        now = timezone.now()
        parent = f'node_{self.node.pk}'
        catalog = {
            parent: {
                'profile': 'keysight_chassis',
                'label': 'AresONE-M 07',
                'metrics': ['cpu_pct', 'mem_pct'],
                'node_id': self.node.pk,
                'chassis_type': 'aresone',
                'mgmt_ip': '192.0.2.37',
            },
            f'{parent}__1.1': {
                'profile': 'keysight_port', 'label': '1.1', 'parent': parent,
                'metrics': ['cpu_pct'], 'node_id': self.node.pk,
            },
            f'{parent}__1.3': {
                'profile': 'keysight_port', 'label': '1.3', 'parent': parent,
                'metrics': ['cpu_pct'], 'node_id': self.node.pk,
            },
        }
        buckets = {
            f'{parent}__1.1': {'cpu_pct': [[now.isoformat(), 4.0]]},
            f'{parent}__1.3': {'cpu_pct': [[(now - timedelta(hours=3)).isoformat(), 9.0]]},
        }
        core = _insight_from_buckets(
            buckets, catalog, topo_id=self.topo.pk, window_minutes=60.0, window_to=now,
        )
        ports = {
            p['label']: p
            for rg in core['pcpu_fleet']
            for p in (rg.get('ports') or [])
        }
        self.assertTrue(ports['1.1']['fresh'])
        self.assertNotIn('1.3', ports)
        self.assertTrue(ports['1.1']['last_sample_at'])

    def test_pcpu_ownership_only_is_not_fresh(self):
        now = timezone.now()
        parent = f'node_{self.node.pk}'
        catalog = {
            parent: {
                'profile': 'keysight_chassis',
                'label': 'AresONE-M 07',
                'metrics': ['cpu_pct', 'mem_pct'],
                'node_id': self.node.pk,
                'chassis_type': 'aresone',
            },
            f'{parent}__1.3': {
                'profile': 'keysight_port', 'label': '1.3', 'parent': parent,
                'metrics': ['port_ownership'], 'node_id': self.node.pk,
            },
        }
        buckets = {
            f'{parent}__1.3': {'port_ownership': [[now.isoformat(), 1.0]]},
        }
        core = _insight_from_buckets(
            buckets, catalog, topo_id=self.topo.pk, window_minutes=60.0, window_to=now,
        )
        ports = {
            p['label']: p
            for rg in core['pcpu_fleet']
            for p in (rg.get('ports') or [])
        }
        self.assertFalse(ports['1.3']['fresh'])
        self.assertIsNone(ports['1.3'].get('last_sample_at'))

    def test_pcpu_last_poll_generation_is_fresh_even_if_old(self):
        now = timezone.now()
        parent = f'node_{self.node.pk}'
        catalog = {
            parent: {
                'profile': 'keysight_chassis',
                'label': 'AresONE-M 07',
                'metrics': ['cpu_pct'],
                'node_id': self.node.pk,
                'chassis_type': 'aresone',
            },
            f'{parent}__1.1': {
                'profile': 'keysight_port', 'label': '1.1', 'parent': parent,
                'metrics': ['cpu_pct'], 'node_id': self.node.pk,
            },
            f'{parent}__1.2': {
                'profile': 'keysight_port', 'label': '1.2', 'parent': parent,
                'metrics': ['cpu_pct'], 'node_id': self.node.pk,
            },
            f'{parent}__1.3': {
                'profile': 'keysight_port', 'label': '1.3', 'parent': parent,
                'metrics': ['cpu_pct'], 'node_id': self.node.pk,
            },
        }
        latest = (now - timedelta(minutes=25)).isoformat()
        older = (now - timedelta(minutes=80)).isoformat()
        buckets = {
            f'{parent}__1.1': {'cpu_pct': [[latest, 4.0]]},
            f'{parent}__1.2': {'cpu_pct': [[latest, 5.0]]},
            f'{parent}__1.3': {'cpu_pct': [[older, 9.0]]},
        }
        core = _insight_from_buckets(
            buckets, catalog, topo_id=self.topo.pk, window_minutes=60.0, window_to=now,
        )
        ports = {
            p['label']: p
            for rg in core['pcpu_fleet']
            for p in (rg.get('ports') or [])
        }
        self.assertTrue(ports['1.1']['fresh'])
        self.assertTrue(ports['1.2']['fresh'])
        self.assertNotIn('1.3', ports)

    def test_pcpu_prunes_leftover_on_its_own_inferred_pcpu(self):
        now = timezone.now()
        parent = f'node_{self.node.pk}'
        catalog = {
            parent: {
                'profile': 'keysight_chassis',
                'label': 'AresONE-M 07',
                'metrics': ['cpu_pct'],
                'node_id': self.node.pk,
                'chassis_type': 'aresone',
            },
            f'{parent}__1.1': {
                'profile': 'keysight_port', 'label': '1.1', 'parent': parent,
                'metrics': ['cpu_pct'], 'node_id': self.node.pk,
            },
            f'{parent}__1.3': {
                'profile': 'keysight_port', 'label': '1.3', 'parent': parent,
                'metrics': ['cpu_pct'], 'node_id': self.node.pk,
                'pcpu_ip': '10.0.1.3',
            },
        }
        latest = now.isoformat()
        older = (now - timedelta(hours=1)).isoformat()
        buckets = {
            f'{parent}__1.1': {'cpu_pct': [[latest, 4.0]]},
            f'{parent}__1.3': {'cpu_pct': [[older, 9.0]]},
        }
        core = _insight_from_buckets(
            buckets, catalog, topo_id=self.topo.pk, window_minutes=60.0, window_to=now,
        )
        labels = [
            p['label']
            for rg in core['pcpu_fleet']
            for p in (rg.get('ports') or [])
        ]
        self.assertIn('1.1', labels)
        self.assertNotIn('1.3', labels)

    @patch('connect.lab_usage_insights.build_enriched_resource_catalog')
    @patch('connect.lab_usage_insights._load_fabric_light')
    @patch('connect.lab_usage_insights.load_port_owner_index')
    def test_pcpu_fleet_ports_include_ownership(self, mock_owners, mock_fabric, mock_catalog):
        mock_fabric.return_value = {'devices': [], 'port_links': [], 'port_nodes': []}
        mock_owners.return_value = {f'node_{self.node.pk}__1.1': 'team-alpha'}
        mock_catalog.return_value = {
            f'node_{self.node.pk}': {
                'profile': 'keysight_chassis',
                'label': 'Test Chassis',
                'metrics': ['cpu_pct', 'mem_pct'],
                'node_id': self.node.pk,
                'node_type': 'chassis',
                'mgmt_ip': '192.0.2.35',
            },
            f'node_{self.node.pk}__1.1': {
                'profile': 'keysight_port',
                'label': '1.1',
                'parent': f'node_{self.node.pk}',
                'metrics': ['cpu_pct', 'mem_pct', 'port_ownership', 'bps_in', 'bps_out'],
                'node_id': self.node.pk,
                'pcpu_ip': '10.0.0.1',
                'chassis_ip': '192.0.2.35',
            },
        }
        now = timezone.now()
        payload = build_usage_insights(self.topo.pk, now - timedelta(hours=1), now)
        fleet = payload['pcpu_fleet']
        self.assertGreaterEqual(len(fleet), 1)
        tile = fleet[0]
        self.assertGreaterEqual(tile.get('owned_count', 0), 1)
        ports = tile.get('ports') or []
        self.assertTrue(ports)
        owned = [p for p in ports if p.get('owned')]
        self.assertTrue(owned)
        self.assertEqual(owned[0].get('owner'), 'team-alpha')
        rank = payload['rankings']['cpu_pct'][0]
        self.assertEqual(rank.get('owner'), 'team-alpha')

    @patch('connect.keysight_drivers.get_driver')
    def test_load_port_pcpu_mgmt_skips_live_ssh_by_default(self, mock_get_driver):
        invalidate_resource_catalog_cache(self.topo.pk)
        load_port_pcpu_mgmt_index(self.topo.pk)
        mock_get_driver.assert_not_called()

    def test_attach_mgmt_ips_infers_pcpu_without_ssh(self):
        catalog = {
            f'node_{self.node.pk}': {
                'profile': 'keysight_chassis',
                'label': 'Chassis',
                'node_id': self.node.pk,
                'mgmt_ip': '10.1.2.3',
            },
            f'node_{self.node.pk}__3.1': {
                'profile': 'keysight_port',
                'label': '3.1',
                'parent': f'node_{self.node.pk}',
            },
        }
        attach_mgmt_ips_to_catalog(catalog, self.topo.pk)
        self.assertEqual(catalog[f'node_{self.node.pk}__3.1'].get('pcpu_ip'), '10.0.3.1')

    def test_link_state_from_events_prefers_latest(self):
        evs = [
            {'event_type': 'link_up', 'started_at': timezone.now()},
            {'event_type': 'link_down', 'started_at': timezone.now()},
        ]
        self.assertFalse(_link_state_from_events(evs))

    def test_resolve_port_link_up_ignores_historical_max(self):
        evs = [{'event_type': 'link_down', 'started_at': timezone.now()}]
        link_s = {'latest': 0.0, 'max': 100.0}
        self.assertFalse(_resolve_port_link_up(evs, link_s))

    @patch('connect.lab_usage_insights.build_enriched_resource_catalog')
    @patch('connect.lab_usage_insights._load_fabric_light')
    def test_rankings_iface_ip_for_switch_ports(self, mock_fabric, mock_catalog):
        """Traffic rankings show parent switch mgmt IP when port has no PCPU host."""
        mock_fabric.return_value = {'devices': [], 'port_links': [], 'port_nodes': []}
        sw_id = self.node.pk + 99
        mock_catalog.return_value = {
            f'node_{sw_id}': {
                'profile': 'arista_switch',
                'label': 'Leaf-1',
                'metrics': ['cpu_pct', 'mem_pct', 'bps_in', 'bps_out'],
                'node_id': sw_id,
                'mgmt_ip': '10.99.1.10',
            },
            f'node_{sw_id}__Ethernet3/3': {
                'profile': 'switch_port',
                'label': 'Ethernet3/3',
                'parent': f'node_{sw_id}',
                'metrics': ['bps_in', 'bps_out'],
                'node_id': sw_id,
                'chassis_ip': '10.99.1.10',
            },
        }
        now = timezone.now()
        LabMetricSample.objects.using('np_timeseries').create(
            topology_id=self.topo.pk,
            resource_key=f'node_{sw_id}__Ethernet3/3',
            metric='bps_in',
            value=186.5e9,
            sampled_at=now,
        )
        payload = build_usage_insights(self.topo.pk, now - timedelta(hours=1), now)
        traffic = payload['rankings'].get('bps_total') or []
        self.assertTrue(traffic)
        self.assertEqual(traffic[0].get('iface_ip'), '10.99.1.10')

    def test_resolve_port_link_up_uses_latest_speed_when_no_events(self):
        link_s = {'latest': 0.0, 'max': 100.0}
        self.assertFalse(_resolve_port_link_up([], link_s))
        link_s_up = {'latest': 10.0, 'max': 10.0}
        self.assertTrue(_resolve_port_link_up([], link_s_up))

    def test_is_fabric_mapped_switch_label(self):
        self.assertFalse(_is_fabric_mapped_switch_label('Ethernet6/1'))
        self.assertTrue(_is_fabric_mapped_switch_label('2.1.1'))

    def test_insight_switch_input_discards(self):
        switch_key = 'node_99'
        raw_key = f'{switch_key}__Ethernet6/1'
        mapped_key = f'{switch_key}__2.1.1'
        catalog = {
            switch_key: {
                'profile': 'arista_switch',
                'label': 'Arista spine',
                'metrics': ['cpu_pct', 'mem_pct'],
                'node_id': 99,
            },
            raw_key: {
                'profile': 'switch_port',
                'label': 'Ethernet6/1',
                'parent': switch_key,
                'metrics': ['bps_in', 'bps_out', 'input_discards'],
            },
            mapped_key: {
                'profile': 'switch_port',
                'label': '2.1.1',
                'parent': switch_key,
                'metrics': ['bps_in', 'bps_out', 'input_discards'],
            },
        }
        buckets = {
            raw_key: {
                'input_discards': [
                    ['2026-06-01T00:00:00+00:00', 0.0],
                    ['2026-06-01T00:05:00+00:00', 4.0],
                    ['2026-06-01T00:10:00+00:00', 2.0],
                ],
            },
        }
        core = _insight_from_buckets(buckets, catalog, topo_id=self.topo.pk, window_minutes=60.0)
        disc = core['switch_input_discards']
        self.assertEqual(len(disc), 1)
        row = disc[0]
        self.assertEqual(row['total_drops'], 6)
        self.assertEqual(row['peak_interval_drops'], 4)
        self.assertEqual(row['first_drop_at'], '2026-06-01T00:05:00+00:00')
        self.assertFalse(row['is_fabric_mapped'])
        self.assertEqual(core['devices'][0]['discard_summary']['total_drops'], 6)

    def test_build_usage_insights_unmocked_without_chassis_port_count(self):
        """Customer SKU KeysightChassis has no port_count — catalog must not crash."""
        now = timezone.now()
        payload = build_usage_insights(self.topo.pk, now - timedelta(hours=1), now)
        self.assertTrue(payload['ok'])
        self.assertGreaterEqual(payload['health']['catalog_size'], 1)

    def test_build_usage_insights_unmocked_switch_only(self):
        topo = create_topology('switch-only-insights')
        create_switch_node(topo, hostname='leaf-1')
        now = timezone.now()
        payload = build_usage_insights(topo.pk, now - timedelta(hours=1), now)
        self.assertTrue(payload['ok'])

    def test_compact_pulse_payload_drops_bulk_and_dummy_rows(self):
        payload = compact_pulse_payload({
            'devices': [{'resource_key': 'node_1', 'pcpus': [{'cpu': 99}]}],
            'pcpu_fleet': [
                {
                    'resource_key': 'node_1',
                    'applications': {'IxOS': '1'},
                    'sample_ports': ['1.1'],
                    'ports': [{
                        'label': '1.1',
                        'owned': True,
                        'speed_gbps': 400,
                        'unused_series': [1, 2, 3],
                    }],
                },
                {
                    'resource_key': 'node_1',
                    'applications': {'IxOS': 'dup'},
                    'ports': [],
                },
            ],
            'dut_drops': [
                {'resource_key': 'dummy-dut', 'drops': 1},
                {'resource_key': 'real-dut', 'drops': 2},
            ],
        })
        self.assertNotIn('pcpus', payload['devices'][0])
        self.assertNotIn('sample_ports', payload['pcpu_fleet'][0])
        self.assertEqual(payload['pcpu_fleet'][0]['applications']['IxOS'], '1')
        self.assertNotIn('applications', payload['pcpu_fleet'][1])
        self.assertEqual(payload['pcpu_fleet'][0]['ports'][0]['speed_gbps'], 400)
        self.assertNotIn('unused_series', payload['pcpu_fleet'][0]['ports'][0])
        self.assertEqual([d['resource_key'] for d in payload['dut_drops']], ['real-dut'])
