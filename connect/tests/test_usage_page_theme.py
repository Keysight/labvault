"""Tests for getdesign.md-inspired usage page themes."""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from connect.models import LabTopology

THEME_IDS = ('carbon', 'nvidia', 'kraken', 'linear', 'posthog')
STATIC_ROOT = Path(__file__).resolve().parents[1] / 'static'


class UsagePageThemeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='usagetheme', password='test')
        self.topo = LabTopology.objects.create(name='ThemeTopo')
        self.client.login(username='usagetheme', password='test')

    def _assert_topbar_theme_assets(self, resp):
        self.assertContains(resp, 'usage-page-theme.css')
        self.assertContains(resp, 'usage-page-theme.js')
        self.assertContains(resp, 'id="lv-usage-theme-picker"')
        self.assertContains(resp, 'mountUsageThemePicker')
        self.assertContains(resp, 'aria-label="Page theme (getdesign.md)"')

    def test_usage_page_loads_with_theme_assets_in_topbar(self):
        url = reverse('lab_topology_usage', args=[self.topo.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self._assert_topbar_theme_assets(resp)
        self.assertContains(resp, 'data-usage-theme="carbon"')
        self.assertNotContains(resp, 'id="ug-theme-picker"')

    def test_insights_page_loads_with_theme_assets_in_topbar(self):
        url = reverse('lab_topology_usage_insights', args=[self.topo.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self._assert_topbar_theme_assets(resp)
        self.assertNotContains(resp, 'd3js.org')
        self.assertContains(resp, 'usage-insights.js?v=20260911e')

    def test_insights_json_returns_warming_without_snapshot(self):
        url = reverse('lab_topology_usage_insights_json', args=[self.topo.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['health']['reason'], 'snapshot_warming')

    def test_designer_page_loads_with_theme_assets_in_topbar(self):
        url = reverse('lab_topology_detail', args=[self.topo.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self._assert_topbar_theme_assets(resp)

    def test_theme_css_defines_all_presets_on_topology_root(self):
        css = (STATIC_ROOT / 'css' / 'usage-page-theme.css').read_text(encoding='utf-8')
        for theme_id in THEME_IDS:
            self.assertIn(f'.lv-topology-page[data-usage-theme="{theme_id}"]', css)

    def test_theme_js_exports_all_presets(self):
        js = (STATIC_ROOT / 'js' / 'lab-graph' / 'usage-page-theme.js').read_text(encoding='utf-8')
        for theme_id in THEME_IDS:
            self.assertIn(f"id: '{theme_id}'", js)
