"""Compose/systemd collector must call the real collect loop in live mode."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from connect.metric_collectors import collect_all_topologies
from connect.tests.timeline_test_helpers import create_chassis_node, create_topology


class RunMetricCollectorTests(TestCase):
    databases = {'default', 'np_timeseries'}

    def test_collect_all_topologies_is_exported(self):
        self.assertTrue(callable(collect_all_topologies))

    @patch('connect.lab_usage_insights.refresh_stale_insights_snapshots')
    @patch('connect.metric_collectors.collect_all_topologies')
    @patch('connect.metric_collectors.collector_mode', return_value='live')
    def test_live_once_invokes_collect_all(self, _mode, mock_collect, mock_snap):
        mock_collect.return_value = 3
        out = StringIO()
        call_command('run_metric_collector', '--once', stdout=out)
        mock_collect.assert_called_once()
        mock_snap.assert_called_once()
        self.assertIn('wrote 3', out.getvalue())

    @patch('connect.lab_usage_insights.refresh_stale_insights_snapshots')
    @patch('connect.metric_collectors.collect_all_topologies')
    @patch('connect.metric_collectors.collector_mode', return_value='idle')
    def test_idle_once_does_not_collect(self, _mode, mock_collect, mock_snap):
        out = StringIO()
        call_command('run_metric_collector', '--once', stdout=out)
        mock_collect.assert_not_called()
        mock_snap.assert_called_once()
        self.assertIn('collector idle', out.getvalue())

    @patch('connect.metric_collectors.collect_topology_node', return_value=1)
    @patch('connect.metric_collectors.collector_mode', return_value='live')
    def test_collect_all_topologies_polls_enabled_nodes(self, _mode, mock_node):
        topo = create_topology('live-collect')
        create_chassis_node(topo)
        n = collect_all_topologies(topo.pk, workers=1)
        self.assertEqual(n, 1)
        mock_node.assert_called_once()
