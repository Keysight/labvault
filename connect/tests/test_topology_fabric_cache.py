from django.test import SimpleTestCase

from connect.topology_fabric_cache import (
    compute_topology_revision,
    django_cache_key,
    site_files_signature,
)


class TestTopologyFabricCache(SimpleTestCase):
    def test_revision_includes_site_sig(self):
        class _Topo:
            updated_at = None

            def nodes(self):
                class _Q:
                    def count(self):
                        return 3

                return _Q()

            def links(self):
                class _Q:
                    def count(self):
                        return 2

                return _Q()

        r1 = compute_topology_revision(_Topo())
        self.assertTrue(r1.startswith('v1:'))
        self.assertIn(site_files_signature(), r1)

    def test_django_cache_key_stable(self):
        k = django_cache_key(
            20, 'v1:1:2:3:abc', api='port_fabric',
            want_live=False, want_lldp=True, force_refresh=False,
        )
        self.assertIn('port_fabric:v8:20:', k)
