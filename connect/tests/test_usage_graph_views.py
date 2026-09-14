"""Tests for graph-based Lab Pulse companion views."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from connect.models import LabTopology


class UsageGraphViewsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='graphview', password='test')
        self.topo = LabTopology.objects.create(name='GraphViewTopo')
        self.client.login(username='graphview', password='test')

    def test_pulse_radial_page_loads(self):
        url = reverse('lab_topology_graph_pulse_radial', args=[self.topo.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Pulse Radial')
        self.assertContains(resp, 'ugv-data-mode')

    def test_stream_wave_page_loads(self):
        url = reverse('lab_topology_graph_stream_wave', args=[self.topo.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Stream Wave')

    def test_radar_health_page_loads(self):
        url = reverse('lab_topology_graph_radar_health', args=[self.topo.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Radar Health')

    def test_matrix_heat_page_loads(self):
        url = reverse('lab_topology_graph_matrix_heat', args=[self.topo.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Matrix Heat')

    def test_topology_pulse_page_loads(self):
        url = reverse('lab_topology_graph_topology_pulse', args=[self.topo.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Topology Pulse')

    def test_insights_subnav_on_lab_pulse(self):
        url = reverse('lab_topology_usage_insights', args=[self.topo.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'lv-insights-subnav')
        self.assertContains(resp, 'ui-data-mode')
