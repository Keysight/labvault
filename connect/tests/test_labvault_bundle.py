import json
import tarfile
import tempfile
from pathlib import Path

from django.test import TestCase

from connect.labvault_bundle import BUNDLE_FORMAT, extract_bundle, import_bundle, write_bundle
from connect.models import KeysightChassis, LabTopology


class LabvaultBundleTests(TestCase):
    def test_ocs_scoped_export_contains_inventory_and_topology(self):
        if not KeysightChassis.objects.filter(lab_name='OCS Lab').exists():
            self.skipTest('No OCS Lab chassis in test DB')
        topo = LabTopology.objects.filter(name__icontains='HBG').first()
        if not topo:
            self.skipTest('No HBG topology in test DB')

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'ocs.tar.gz'
            manifest = write_bundle(
                out,
                lab='OCS Lab',
                ip_prefix='10.36.84.',
                topology_ids=[topo.pk],
                include_lldp_cache=False,
            )
            self.assertTrue(out.is_file())
            self.assertEqual(manifest['format'], BUNDLE_FORMAT)

            with tarfile.open(out, 'r:gz') as tar:
                names = tar.getnames()
            self.assertIn('manifest.json', names)
            self.assertIn('inventory.json', names)
            self.assertTrue(any(n.startswith('topologies/') for n in names))
            self.assertTrue(any(n.startswith('resources/') for n in names))

            root = extract_bundle(out, Path(tmp) / 'extracted')
            inv = json.loads((root / 'inventory.json').read_text(encoding='utf-8'))
            self.assertGreaterEqual(len(inv.get('keysight_chassis', [])), 1)
            self.assertGreaterEqual(len(inv.get('devices', [])), 1)

    def test_round_trip_import_dry_topology_name(self):
        topo = LabTopology.objects.create(
            name='Bundle Test Topology',
            description='ephemeral',
            source='test',
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'mini.tar.gz'
            write_bundle(
                out,
                topology_ids=[topo.pk],
                include_lldp_cache=False,
            )
            topo.delete()
            stats = import_bundle(out, import_lldp_cache=False)
            self.assertTrue(stats.get('topologies'))
            restored = LabTopology.objects.filter(name='Bundle Test Topology').first()
            self.assertIsNotNone(restored)
            restored.delete()
