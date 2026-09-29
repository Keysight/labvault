"""Tests for OCS triplet enrichment from site JSON."""
from django.test import SimpleTestCase

from connect.lab_topology_io import _enrich_extra_from_site


class EnrichExtraFromSiteTests(SimpleTestCase):
    def test_derives_ocs_triplet_map_from_site(self):
        extra = {}
        site_info = {
            'fixed_mapping': {
                'port_to_ocs_triplets': {'49': ['1-1-1', '1-1-2', '1-1-3']},
            },
        }
        _enrich_extra_from_site(extra, '192.0.2.40', site_info)
        self.assertEqual(extra['ocs_triplet_map']['49'], ['1-1-1', '1-1-2', '1-1-3'])
