"""Fleet API helpers — shared cache port summaries."""

from django.test import SimpleTestCase

from connect.fleet_api_views import _cached_port_fields


class FleetCachedPortFieldsTests(SimpleTestCase):
    def test_derives_counts_from_ports_list(self):
        cached = {
            'ports': [
                {'link_state': 'up', 'owner': 'Free'},
                {'link_state': 'down', 'owner': 'team-a'},
                {'link_state': 'up', 'owner': 'Free'},
            ],
        }
        out = _cached_port_fields(cached)
        self.assertEqual(out['total_ports'], 3)
        self.assertEqual(out['ports_up'], 2)
        self.assertEqual(out['ports_free'], 2)

    def test_prefers_explicit_summary_fields(self):
        cached = {
            'ports_up': 9,
            'ports_free': 4,
            'total_ports': 16,
            'ports': [{'link_state': 'up', 'owner': 'Free'}],
        }
        out = _cached_port_fields(cached)
        self.assertEqual(out['ports_up'], 9)
        self.assertEqual(out['ports_free'], 4)
        self.assertEqual(out['total_ports'], 16)
