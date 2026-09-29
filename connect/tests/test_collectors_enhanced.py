"""Enhanced collector tests: IxOS metrics, KCOS stats, parallel isolation."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from connect.keysight_drivers.ixos import DriverResult
from connect.metric_collectors import (
    CollectorState,
    _counter_delta,
    _index_port_stats,
    _lookup_port_stat,
    _parse_speed_gbps,
    _port_throughput_bps,
    collect_chassis,
    collect_switch,
    collect_topology_node,
)
from connect.models import Device, LabMetricSample, LabResourceEvent, LabTopologyNode
from connect.tests.timeline_test_helpers import create_chassis_node, create_topology


class EnhancedCollectorTests(TestCase):
    databases = {'default', 'np_timeseries'}

    def setUp(self):
        self.topo = create_topology('collector-test')
        self.node = create_chassis_node(self.topo)
        self.state = CollectorState()
        self.chassis = self.node.extra  # chassis_id only; load below

    def _chassis(self):
        from connect.models import KeysightChassis
        return KeysightChassis.objects.get(pk=self.node.extra['chassis_id'])

    @patch('connect.metric_collectors._get_chassis_driver_cached')
    def test_ixos_writes_link_speed_and_ownership(self, mock_drv):
        driver = MagicMock()
        mock_drv.return_value = driver
        driver.get_health.return_value = DriverResult(success=True, data={
            'cpu_utilization': 5.0,
            'memory_percent': 40.0,
        })
        driver.get_ports.return_value = DriverResult(success=True, data=[{
            'card_number': 1,
            'port_number': 1,
            'owner': 'team-a',
            'link_state': 'UP',
            'speed': '100G',
        }])
        driver.get_port_stats.return_value = DriverResult(success=True, data=[{
            'cardNumber': 1,
            'portNumber': 1,
            'rxBitRate': 0,
            'txBitRate': 0,
        }])

        n = collect_chassis(self.topo.pk, self.node, self._chassis(), self.state)
        self.state.event_buffer.flush()
        self.assertGreater(n, 0)

        metrics = set(
            LabMetricSample.objects.using('np_timeseries').filter(
                topology_id=self.topo.pk,
            ).values_list('metric', flat=True)
        )
        self.assertIn('port_ownership', metrics)
        self.assertIn('link_speed_gbps', metrics)
        self.assertIn('bps_in', metrics)

        events = LabResourceEvent.objects.using('np_timeseries').filter(
            topology_id=self.topo.pk,
        )
        types = set(events.values_list('event_type', flat=True))
        self.assertIn('port_owned', types)
        self.assertIn('link_up', types)

    @patch('connect.metric_collectors._get_chassis_driver_cached')
    def test_ixos_port_stats_cpu_mem_when_present(self, mock_drv):
        driver = MagicMock()
        mock_drv.return_value = driver
        driver.get_health.return_value = DriverResult(success=True, data={'cpu_utilization': 5.0})
        driver.get_ports.return_value = DriverResult(success=True, data=[{
            'card_number': 2,
            'port_number': 3,
            'owner': 'Free',
            'link_state': 'DOWN',
        }])
        driver.get_port_stats.return_value = DriverResult(success=True, data=[{
            'cardNumber': 2,
            'portNumber': 3,
            'rxBitRate': 1000,
            'txBitRate': 2000,
            'cpuUsagePercent': 82.5,
            'memoryUsagePercent': 61.0,
        }])
        n = collect_chassis(self.topo.pk, self.node, self._chassis(), self.state)
        self.assertGreater(n, 0)
        prkey = f'node_{self.node.pk}__2.3'
        rows = LabMetricSample.objects.using('np_timeseries').filter(
            topology_id=self.topo.pk, resource_key=prkey,
        ).values_list('metric', 'value')
        by_metric = {m: v for m, v in rows}
        self.assertAlmostEqual(by_metric.get('cpu_pct'), 82.5)
        self.assertAlmostEqual(by_metric.get('mem_pct'), 61.0)

    @patch('connect.metric_collectors._get_chassis_driver_cached')
    def test_ixos_port_memory_and_owned_cpu_fallback(self, mock_drv):
        driver = MagicMock()
        mock_drv.return_value = driver
        driver.get_health.return_value = DriverResult(success=True, data={
            'cpu_utilization': 12.5,
            'memory_used': 1,
            'memory_total': 4,
        })
        driver.get_ports.return_value = DriverResult(success=True, data=[
            {
                'card_number': 1,
                'port_number': 1,
                'owner': 'BreakingPoint/admin',
                'link_state': 'UP',
                'port_memory_kb': 8192,
            },
            {
                'card_number': 1,
                'port_number': 2,
                'owner': 'Free',
                'link_state': 'DOWN',
                'port_memory_kb': 4096,
            },
        ])
        driver.get_port_stats.return_value = DriverResult(success=True, data=[{
            'cardNumber': 1,
            'portNumber': 1,
            'rxBitRate': 0,
            'txBitRate': 0,
        }])
        n = collect_chassis(self.topo.pk, self.node, self._chassis(), self.state)
        self.assertGreater(n, 0)
        owned = f'node_{self.node.pk}__1.1'
        free = f'node_{self.node.pk}__1.2'
        owned_rows = dict(LabMetricSample.objects.using('np_timeseries').filter(
            topology_id=self.topo.pk, resource_key=owned,
        ).values_list('metric', 'value'))
        self.assertAlmostEqual(owned_rows.get('cpu_pct'), 12.5)
        self.assertAlmostEqual(owned_rows.get('mem_pct'), 25.0)
        free_rows = dict(LabMetricSample.objects.using('np_timeseries').filter(
            topology_id=self.topo.pk, resource_key=free,
        ).values_list('metric', 'value'))
        self.assertAlmostEqual(free_rows.get('cpu_pct'), 12.5)
        self.assertAlmostEqual(free_rows.get('mem_pct'), 25.0)

    @patch('connect.metric_collectors._get_chassis_driver_cached')
    def test_ixos_pcpu_health_overrides_chassis_fallback(self, mock_drv):
        driver = MagicMock()
        mock_drv.return_value = driver
        driver.get_health.return_value = DriverResult(success=True, data={
            'cpu_utilization': 12.5,
            'memory_used': 1,
            'memory_total': 4,
        })
        driver.get_ports.return_value = DriverResult(success=True, data=[{
            'card_number': 2,
            'port_number': 1,
            'port_display': '2.1',
            'owner': 'Free',
            'link_state': 'UP',
            'management_ip': '10.0.1.1',
        }])
        driver.get_port_stats.return_value = DriverResult(success=True, data=[])
        driver.get_pcpu_health_by_mgmt_ip.return_value = DriverResult(success=True, data={
            '10.0.1.1': {'cpu_pct': 44.0, 'mem_pct': 55.0},
        })
        collect_chassis(self.topo.pk, self.node, self._chassis(), self.state)
        prkey = f'node_{self.node.pk}__2.1'
        rows = dict(LabMetricSample.objects.using('np_timeseries').filter(
            topology_id=self.topo.pk, resource_key=prkey,
        ).values_list('metric', 'value'))
        self.assertAlmostEqual(rows.get('cpu_pct'), 44.0)
        self.assertAlmostEqual(rows.get('mem_pct'), 55.0)

    def test_ixos_speed_mbps_to_gbps(self):
        self.assertAlmostEqual(_parse_speed_gbps(400000), 400.0)
        self.assertAlmostEqual(_parse_speed_gbps('100000'), 100.0)
        self.assertAlmostEqual(_parse_speed_gbps('100G'), 100.0)

    def test_ixos_portstats_parent_id_lookup(self):
        port = {'id': 1216, 'card_number': 1, 'port_number': 131}
        stats = [{
            'id': 1844,
            'parentId': 1216,
            'bytesReceived': 1_000_000,
            'bytesSent': 500_000,
        }]
        by_id, by_cp, by_parent = _index_port_stats(stats)
        stat = _lookup_port_stat(port, by_id, by_cp, by_parent)
        self.assertIsNotNone(stat)
        self.assertEqual(stat['parentId'], 1216)

    def test_ixos_portstats_byte_counter_delta_bps(self):
        state = CollectorState()
        prkey = 'node_1__2.1'
        now = timezone.now()
        stat = {'bytesReceived': 1_000_000, 'bytesSent': 0}
        rx1, tx1, m1 = _port_throughput_bps(stat, prkey=prkey, now=now, state=state)
        self.assertEqual(rx1, 0.0)
        self.assertFalse(m1)
        later = now + timedelta(seconds=10)
        stat2 = {'bytesReceived': 2_500_000, 'bytesSent': 0}
        rx2, tx2, m2 = _port_throughput_bps(stat2, prkey=prkey, now=later, state=state)
        self.assertAlmostEqual(rx2, 1_200_000.0)  # 1.5M bytes * 8 / 10s
        self.assertEqual(tx2, 0.0)
        self.assertTrue(m2)

    def test_ixos_byte_counter_reset_reseeds_without_spike(self):
        state = CollectorState()
        prkey = 'node_1__1.1'
        now = timezone.now()
        _port_throughput_bps(
            {'bytesReceived': 900_000_000_000, 'bytesSent': 0},
            prkey=prkey, now=now, state=state,
        )
        later = now + timedelta(seconds=300)
        rx, tx, measured = _port_throughput_bps(
            {'bytesReceived': 1_000_000, 'bytesSent': 0},
            prkey=prkey, now=later, state=state,
        )
        self.assertEqual(rx, 0.0)
        self.assertEqual(tx, 0.0)
        self.assertFalse(measured)

    def test_collector_state_persists_if_counters(self):
        from django.core.cache import cache

        cache.delete('collector:if_counters:v1')
        state = CollectorState()
        now = timezone.now()
        state.if_counters['node_1__1.1'] = {'bytes_in': 100, 'bytes_out': 0}
        state.if_counter_ts['node_1__1.1'] = now
        state.persist_if_counters()

        restored = CollectorState()
        restored.load_if_counters()
        self.assertEqual(restored.if_counters['node_1__1.1']['bytes_in'], 100)
        self.assertEqual(restored.if_counter_ts['node_1__1.1'], now)

    @patch('connect.metric_collectors._get_chassis_driver_cached')
    def test_ixos_aresone_parent_id_writes_bps(self, mock_drv):
        driver = MagicMock()
        mock_drv.return_value = driver
        driver.get_health.return_value = DriverResult(success=True, data={
            'cpu_utilization': 5.0,
            'memory_percent': 40.0,
        })
        driver.get_ports.return_value = DriverResult(success=True, data=[{
            'id': 1216,
            'card_number': 1,
            'port_number': 131,
            'port_display': '2.1',
            'owner': 'IxNetwork/test',
            'link_state': 'UP',
        }])
        driver.get_port_stats.return_value = DriverResult(success=True, data=[{
            'id': 1844,
            'parentId': 1216,
            'bytesReceived': 0,
            'bytesSent': 0,
        }])
        collect_chassis(self.topo.pk, self.node, self._chassis(), self.state)
        driver.get_port_stats.return_value = DriverResult(success=True, data=[{
            'id': 1844,
            'parentId': 1216,
            'bytesReceived': 10_000_000,
            'bytesSent': 5_000_000,
        }])
        collect_chassis(self.topo.pk, self.node, self._chassis(), self.state)
        prkey = f'node_{self.node.pk}__2.1'
        bps_in = (
            LabMetricSample.objects.using('np_timeseries')
            .filter(topology_id=self.topo.pk, resource_key=prkey, metric='bps_in')
            .order_by('-sampled_at')
            .values_list('value', flat=True)
            .first()
        )
        self.assertIsNotNone(bps_in)
        self.assertGreater(bps_in, 0)

    @patch('connect.metric_collectors._get_chassis_driver_cached')
    def test_ixos_port_display_label_for_metrics(self, mock_drv):
        driver = MagicMock()
        mock_drv.return_value = driver
        driver.get_health.return_value = DriverResult(success=True, data={
            'cpu_utilization': 9.0,
            'memory_used': 1,
            'memory_total': 2,
        })
        driver.get_ports.return_value = DriverResult(success=True, data=[{
            'card_number': 1,
            'port_number': 11,
            'port_display': '2.1',
            'owner': 'Free',
            'link_state': 'UP',
        }])
        driver.get_port_stats.return_value = DriverResult(success=True, data=[])
        collect_chassis(self.topo.pk, self.node, self._chassis(), self.state)
        prkey = f'node_{self.node.pk}__1.2.1'
        self.assertTrue(
            LabMetricSample.objects.using('np_timeseries').filter(
                topology_id=self.topo.pk, resource_key=prkey, metric='cpu_pct',
            ).exists()
        )

    @patch('connect.metric_collectors._get_chassis_driver_cached')
    def test_kcos_port_stats_from_nic_info(self, mock_drv):
        from connect.keysight_drivers.kcos import KCOSDriver

        driver = KCOSDriver(ip='10.0.0.1', username='u', password='p')
        with patch.object(driver, 'get_aggregated_nic_info') as mock_nic:
            mock_nic.return_value = DriverResult(success=True, data={
                'nodes': [{
                    'nodeName': 'node1',
                    'interfaces': [{
                        'slot': 1,
                        'port': 2,
                        'driverStats': {'rxBitRate': 1000, 'txBitRate': 2000},
                    }],
                }],
            })
            result = driver.get_port_stats()
        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(result.data[0]['rxBitRate'], 1000)

    @patch('connect.metric_collectors.collect_chassis')
    @patch('connect.metric_collectors.collect_switch')
    def test_parallel_node_isolation(self, mock_switch, mock_chassis):
        mock_chassis.side_effect = [10, RuntimeError('device timeout')]
        from connect.models import LabTopologyNode

        node_ok = self.node
        node_fail = LabTopologyNode.objects.create(
            topology=self.topo,
            node_type='chassis',
            label='fail-chassis',
            extra=self.node.extra,
        )
        state = CollectorState()
        results = []
        for node in (node_ok, node_fail):
            try:
                results.append(collect_topology_node(self.topo.pk, node, state))
            except RuntimeError:
                results.append(-1)
        self.assertEqual(results[0], 10)
        self.assertEqual(results[1], -1)

    def test_counter_delta_handles_reset(self):
        self.assertEqual(_counter_delta(10, 15), 5)
        self.assertEqual(_counter_delta(15, 3), 3)
        self.assertEqual(_counter_delta(0, 0), 0)

    @patch('connect.metric_collectors.get_switch_driver')
    def test_collect_switch_input_discards_delta(self, mock_get_driver):
        arista = Device.objects.create(
            hostname='sw-disc', ip_address='192.0.2.99', vendor_type='arista',
        )
        switch_node = LabTopologyNode.objects.create(
            topology=self.topo, label='Arista discard', node_type='switch', device=arista,
        )
        driver = MagicMock()
        mock_get_driver.return_value = driver
        driver.get_health.return_value = DriverResult(success=True, data={
            'cpu_utilization': 1.0,
            'memory_percent': 10.0,
        })
        now = timezone.now()
        driver.get_interface_counters.return_value = DriverResult(success=True, data=[{
            'name': 'Ethernet6/1',
            'bytes_in': 1000,
            'bytes_out': 2000,
            'input_discards': 10,
        }])
        collect_switch(self.topo.pk, switch_node, self.state, sampled_at=now)
        driver.get_interface_counters.return_value = DriverResult(success=True, data=[{
            'name': 'Ethernet6/1',
            'bytes_in': 2000,
            'bytes_out': 3000,
            'input_discards': 15,
        }])
        collect_switch(self.topo.pk, switch_node, self.state, sampled_at=now + timedelta(minutes=5))
        self.state.event_buffer.flush()
        prkey = f'node_{switch_node.pk}__Ethernet6/1'
        disc = (
            LabMetricSample.objects.using('np_timeseries')
            .filter(topology_id=self.topo.pk, resource_key=prkey, metric='input_discards')
            .values_list('value', flat=True)
            .first()
        )
        self.assertEqual(disc, 5.0)
        self.assertTrue(
            LabResourceEvent.objects.using('np_timeseries').filter(
                topology_id=self.topo.pk, resource_key=prkey, event_type='input_discard',
            ).exists()
        )
