"""Tests for lab timeline metrics, catalog, and OCS diff."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from connect.lab_metrics import aggregate_metric_buckets, close_open_events, open_resource_event
from connect.metric_collectors import CollectorState, _normalize_ocs_xconns, collect_ocs
from connect.models import (
    Device,
    LabMetricSample,
    LabResourceEvent,
    LabTopology,
    LabTopologyNode,
)
from connect.topology_resource_catalog import (
    PROFILE_ARISTA_SWITCH,
    PROFILE_KEYSIGHT_CHASSIS,
    PROFILE_OCS,
    alias_switch_metric_buckets,
    build_resource_catalog,
    build_switch_counter_aliases,
    node_resource_key,
)


class LabMetricAggregationTests(TestCase):
    databases = {'default', 'np_timeseries'}

    def setUp(self):
        self.topo = LabTopology.objects.create(name='timeline-test')
        self.now = timezone.now()

    def test_aggregate_metric_buckets_averages(self):
        rkey = f'node_{self.topo.pk}'
        ts = self.now - timedelta(minutes=1)
        LabMetricSample.objects.using('np_timeseries').create(
            topology_id=self.topo.pk,
            resource_key=rkey,
            metric='cpu_pct',
            value=10.0,
            sampled_at=ts,
        )
        LabMetricSample.objects.using('np_timeseries').create(
            topology_id=self.topo.pk,
            resource_key=rkey,
            metric='cpu_pct',
            value=30.0,
            sampled_at=ts,
        )
        window_from = self.now - timedelta(hours=1)
        buckets = aggregate_metric_buckets(
            self.topo.pk, window_from, self.now + timedelta(minutes=1), bucket='5m',
        )
        self.assertIn(rkey, buckets)
        self.assertIn('cpu_pct', buckets[rkey])
        self.assertEqual(len(buckets[rkey]['cpu_pct']), 1)
        self.assertEqual(buckets[rkey]['cpu_pct'][0][1], 20.0)

    def test_resource_event_open_close(self):
        rkey = 'node_1__1.1'
        open_resource_event(self.topo.pk, rkey, 'port_owned', payload={'owner': 'team'})
        self.assertEqual(
            LabResourceEvent.objects.using('np_timeseries').filter(
                topology_id=self.topo.pk, ended_at__isnull=True,
            ).count(),
            1,
        )
        close_open_events(self.topo.pk, rkey, 'port_owned')
        ev = LabResourceEvent.objects.using('np_timeseries').get(topology_id=self.topo.pk)
        self.assertIsNotNone(ev.ended_at)


class ResourceCatalogTests(TestCase):
    databases = {'default', 'np_timeseries'}

    def test_profiles_switch_and_ocs(self):
        topo = LabTopology.objects.create(name='catalog-test')
        arista = Device.objects.create(
            hostname='sw1',
            ip_address='10.0.0.1',
            vendor_type='arista',
        )
        ocs = Device.objects.create(
            hostname='ocs1',
            ip_address='10.0.0.2',
            vendor_type='ocs',
        )
        sw_node = LabTopologyNode.objects.create(
            topology=topo, label='SW1', node_type='switch', device=arista,
        )
        ocs_node = LabTopologyNode.objects.create(
            topology=topo, label='OCS1', node_type='ocs', device=ocs,
        )
        catalog = build_resource_catalog(topo.pk)
        self.assertEqual(catalog[node_resource_key(sw_node)]['profile'], PROFILE_ARISTA_SWITCH)
        self.assertEqual(catalog[node_resource_key(ocs_node)]['profile'], PROFILE_OCS)

    def test_chassis_profile(self):
        topo = LabTopology.objects.create(name='chassis-catalog')
        node = LabTopologyNode.objects.create(
            topology=topo, label='Ixia', node_type='chassis',
            extra={'chassis_id': 99, 'ports': ['1.1', '1.2']},
        )
        catalog = build_resource_catalog(topo.pk)
        self.assertEqual(catalog[node_resource_key(node)]['profile'], PROFILE_KEYSIGHT_CHASSIS)
        self.assertIn(f'node_{node.pk}__1.1', catalog)

    @patch('connect.topology_resource_catalog._live_chassis_port_labels')
    def test_build_catalog_fast_skips_live_chassis_ssh(self, mock_live):
        topo = LabTopology.objects.create(name='fast-catalog')
        LabTopologyNode.objects.create(
            topology=topo, label='Ixia', node_type='chassis',
            extra={'chassis_id': 99, 'ports': ['1.1', '1.2']},
        )
        from connect.topology_resource_catalog import build_enriched_resource_catalog

        catalog = build_enriched_resource_catalog(topo.pk, live_ports=False, use_cache=False)
        mock_live.assert_not_called()
        self.assertIn('node_', next(iter(catalog)))

    def test_enrich_catalog_filters_stale_observed_port_keys(self):
        topo = LabTopology.objects.create(name='enrich-catalog-filter')
        node = LabTopologyNode.objects.create(
            topology=topo, label='Ixia', node_type='chassis',
            extra={'chassis_id': 99, 'ports': ['1', '2']},
        )
        rkey = f'node_{node.pk}__6.1'
        LabMetricSample.objects.using('np_timeseries').create(
            topology_id=topo.pk,
            resource_key=rkey,
            metric='port_ownership',
            value=1.0,
            sampled_at=timezone.now(),
        )
        catalog = build_resource_catalog(topo.pk)
        from connect.topology_resource_catalog import enrich_catalog_with_observed_keys

        enriched = enrich_catalog_with_observed_keys(catalog, topo.pk)
        self.assertNotIn(rkey, enriched)

    def test_enrich_catalog_adds_observed_port_keys_without_live_labels(self):
        topo = LabTopology.objects.create(name='enrich-catalog-add')
        node = LabTopologyNode.objects.create(
            topology=topo, label='Ixia', node_type='chassis',
            extra={'chassis_id': 99},
        )
        rkey = f'node_{node.pk}__6.1'
        LabMetricSample.objects.using('np_timeseries').create(
            topology_id=topo.pk,
            resource_key=rkey,
            metric='port_ownership',
            value=1.0,
            sampled_at=timezone.now(),
        )
        catalog = build_resource_catalog(topo.pk)
        self.assertNotIn(rkey, catalog)
        from connect.topology_resource_catalog import enrich_catalog_with_observed_keys

        enriched = enrich_catalog_with_observed_keys(catalog, topo.pk)
        self.assertIn(rkey, enriched)
        self.assertEqual(enriched[rkey]['label'], '6.1')
        self.assertIn('port_ownership', enriched[rkey]['metrics'])


    def test_chassis_placeholders_use_observed_port_labels(self):
        topo = LabTopology.objects.create(name='chassis-observed')
        node = LabTopologyNode.objects.create(
            topology=topo, label='Ares', node_type='chassis',
            extra={'chassis_id': 99, 'ports': ['port_1', 'port_2']},
        )
        for label in ('1.1', '1.2', '6.3'):
            LabMetricSample.objects.using('np_timeseries').create(
                topology_id=topo.pk,
                resource_key=f'node_{node.pk}__{label}',
                metric='bps_in',
                value=1.0,
                sampled_at=timezone.now(),
            )
        from connect.topology_resource_catalog import build_enriched_resource_catalog

        catalog = build_enriched_resource_catalog(topo.pk, live_ports=False, use_cache=False)
        self.assertIn(f'node_{node.pk}__1.1', catalog)
        self.assertIn(f'node_{node.pk}__6.3', catalog)
        self.assertNotIn(f'node_{node.pk}__port_1', catalog)

    def test_enrich_allows_observed_when_designer_ports_are_placeholders(self):
        topo = LabTopology.objects.create(name='chassis-placeholder-enrich')
        node = LabTopologyNode.objects.create(
            topology=topo, label='Ares', node_type='chassis',
            extra={'chassis_id': 99, 'ports': ['port_1', 'port_2']},
        )
        rkey = f'node_{node.pk}__6.1'
        LabMetricSample.objects.using('np_timeseries').create(
            topology_id=topo.pk,
            resource_key=rkey,
            metric='port_ownership',
            value=1.0,
            sampled_at=timezone.now(),
        )
        from connect.topology_resource_catalog import enrich_catalog_with_observed_keys

        catalog = build_resource_catalog(topo.pk, live_ports=False)
        enriched = enrich_catalog_with_observed_keys(catalog, topo.pk)
        self.assertIn(rkey, enriched)

    def test_switch_counter_aliases_fallback_port_placeholders(self):
        topo = LabTopology.objects.create(name='switch-port-n')
        arista = Device.objects.create(
            hostname='ar1', ip_address='10.36.84.21', vendor_type='arista',
        )
        node = LabTopologyNode.objects.create(
            topology=topo, label='Arista 1', node_type='switch', device=arista,
            extra={'ports': ['port_17', 'port_18']},
        )
        aliases = build_switch_counter_aliases(node)
        self.assertIn('port_17', aliases.get('Ethernet3/1', []))
        self.assertIn('port_17', aliases.get('Ethernet17', []))

    def test_switch_counter_aliases_map_fabric_labels(self):
        topo = LabTopology.objects.create(name='switch-alias')
        arista = Device.objects.create(
            hostname='ar3', ip_address='10.36.84.23', vendor_type='arista',
        )
        node = LabTopologyNode.objects.create(
            topology=topo, label='Arista 3', node_type='switch', device=arista,
            extra={
                'ports': ['2.1.1', '2.1.2'],
                'port_details': [
                    {'name': '2.1.1', 'port_number': 17, 'logical_port': 'port_17'},
                    {'name': '2.1.2', 'port_number': 17, 'logical_port': 'port_17'},
                ],
            },
        )
        aliases = build_switch_counter_aliases(node)
        self.assertEqual(aliases.get('Ethernet3/1'), ['2.1.1', '2.1.2'])

        src_key = f'node_{node.pk}__Ethernet3/1'
        dst_key = f'node_{node.pk}__2.1.1'
        series = {'bps_in': [['2026-06-01T00:00:00+00:00', 42.0]]}
        aliased = alias_switch_metric_buckets({src_key: series}, topo.pk)
        self.assertEqual(aliased[dst_key]['bps_in'], series['bps_in'])

        series_disc = {
            'input_discards': [['2026-06-01T00:05:00+00:00', 3.0], ['2026-06-01T00:10:00+00:00', 7.0]],
        }
        aliased_disc = alias_switch_metric_buckets({src_key: series_disc}, topo.pk)
        self.assertEqual(aliased_disc[dst_key]['input_discards'], series_disc['input_discards'])


class OcsDiffTests(TestCase):
    databases = {'default', 'np_timeseries'}

    def test_normalize_and_collect_patch_add(self):
        rows = [{'name': 'xc1', 'port_a': '2.1.1', 'port_b': '3.1.1'}]
        norm = _normalize_ocs_xconns(rows)
        self.assertIn('xc1', norm)
        self.assertEqual(norm['xc1']['port_b'], '3.1.1')

    @patch('connect.metric_collectors.get_switch_driver')
    def test_collect_ocs_emits_patch_add(self, mock_driver):
        topo = LabTopology.objects.create(name='ocs-diff')
        device = Device.objects.create(hostname='ocs', ip_address='10.1.1.1', vendor_type='ocs')
        node = LabTopologyNode.objects.create(
            topology=topo, label='OCS', node_type='ocs', device=device,
        )
        drv = MagicMock()
        drv.get_ocs_crossconnects.return_value = MagicMock(
            success=True,
            data=[{'name': 'xc1', 'port_a': '1.1.1', 'port_b': '2.2.2'}],
        )
        mock_driver.return_value = drv
        state = CollectorState()
        collect_ocs(topo.pk, node, state)
        state.event_buffer.flush()
        self.assertEqual(
            LabResourceEvent.objects.using('np_timeseries').filter(
                topology_id=topo.pk, event_type='patch_add',
            ).count(),
            1,
        )
        self.assertIn(node.pk, state.ocs_xconns)
