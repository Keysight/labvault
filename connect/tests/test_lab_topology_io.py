"""Tests for consolidated lab topology import/export."""
from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase

from connect.lab_topology_io import export_topology, import_topology, resource_paths_for_profile
from connect.models import LabTopology, LabTopologyLink, LabTopologyNode


class LabTopologyIoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('topo_user', password='x')
        self.topo = LabTopology.objects.create(
            name='Test Topo',
            description='desc',
            tags='test',
            extra={
                'addressing_profile': 'v6',
                'view_layouts': {
                    'port_fabric': {
                        'pos': {'node_1': {'x': 10, 'y': 20}},
                        'layout_mode': 'dc',
                    },
                },
            },
            created_by=self.user,
        )
        self.n_ocs = LabTopologyNode.objects.create(
            topology=self.topo,
            node_key='ocs',
            node_type='ocs',
            label='OCS',
            x=100,
            y=200,
            extra={'device_ip': '10.36.84.39', 'preferred_ip_version': 'ipv6'},
        )
        self.n_sw = LabTopologyNode.objects.create(
            topology=self.topo,
            node_key='arista1',
            node_type='switch',
            label='Arista1',
            x=50,
            y=50,
            extra={'device_ip': '10.36.84.21'},
        )
        LabTopologyLink.objects.create(
            topology=self.topo,
            node_a=self.n_ocs,
            port_a='shelf-1',
            node_b=self.n_sw,
            port_b='port_9',
            cable_type='ocs',
        )

    def test_export_has_stable_ids_and_view_layout_keys(self):
        payload = export_topology(self.topo)
        self.assertEqual(payload['format'], 'labvault.lab_topology')
        self.assertEqual(payload['version'], 3)
        ids = {n['id'] for n in payload['nodes']}
        self.assertIn('ocs', ids)
        self.assertIn('arista1', ids)
        pf = payload['view_layouts']['port_fabric']
        self.assertIn('ocs', pf['pos_by_node_key'])
        self.assertEqual(pf['pos_by_node_key']['ocs'], {'x': 10, 'y': 20})

    def test_import_preserves_view_layouts_after_node_recreate(self):
        payload = export_topology(self.topo)
        old_ocs_pk = self.n_ocs.pk
        msg = import_topology(self.topo, payload)
        self.assertIn('views preserved', msg)
        self.topo.refresh_from_db()
        new_ocs = self.topo.nodes.get(node_key='ocs')
        self.assertNotEqual(new_ocs.pk, old_ocs_pk)
        layouts = (self.topo.extra or {}).get('view_layouts') or {}
        pos = layouts['port_fabric']['pos']
        self.assertIn(f'node_{new_ocs.pk}', pos)
        self.assertEqual(pos[f'node_{new_ocs.pk}'], {'x': 10, 'y': 20})

    def test_v6_resource_paths(self):
        layout, site = resource_paths_for_profile('v6')
        self.assertEqual(layout, 'lab_topology_schema_v6.json')
        self.assertEqual(site, 'ocs_photonic_site.json')
