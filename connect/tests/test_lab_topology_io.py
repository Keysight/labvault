"""Tests for consolidated lab topology import/export."""
from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase

from connect.lab_topology_io import export_topology, import_topology, resource_paths_for_profile
from connect.metric_collectors import node_is_collectable
from connect.models import Device, KeysightChassis, LabTopology, LabTopologyLink, LabTopologyNode


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

    def test_import_binds_chassis_when_device_shares_ip(self):
        """Pickup JSON lists AresONE as Device + KeysightChassis at the same IP."""
        Device.objects.create(
            hostname='AresONE-01',
            ip_address='192.0.2.31',
            username='admin',
            password='admin',
            vendor_type='keysight',
        )
        chassis = KeysightChassis.objects.create(
            hostname='AresONE-01',
            ip_address='192.0.2.31',
            username='admin',
            password='admin',
            chassis_type='aresone',
        )
        self.topo.metrics_collection_enabled = False
        self.topo.save(update_fields=['metrics_collection_enabled'])
        payload = {
            'format': 'labvault.lab_topology',
            'version': 3,
            'topology': {'name': self.topo.name},
            'nodes': [{
                'id': 'aresone01',
                'node_key': 'aresone01',
                'label': 'AresONE-01',
                'node_type': 'chassis',
                'device_ip': '192.0.2.31',
                'extra': {'chassis_id': 99999},
            }],
            'links': [],
        }
        import_topology(self.topo, payload)
        self.topo.refresh_from_db()
        node = self.topo.nodes.get(node_key='aresone01')
        self.assertEqual(node.extra.get('chassis_id'), chassis.pk)
        self.assertEqual(node.extra.get('device_ip'), '192.0.2.31')
        self.assertTrue(self.topo.metrics_collection_enabled)
        self.assertTrue(node_is_collectable(node))

    def test_collectable_falls_back_to_ip_when_chassis_pk_is_stale(self):
        chassis = KeysightChassis.objects.create(
            hostname='AresONE-02',
            ip_address='192.0.2.32',
            username='admin',
            password='admin',
            chassis_type='aresone',
        )
        node = LabTopologyNode.objects.create(
            topology=self.topo,
            node_key='stale-chassis',
            node_type='chassis',
            label='AresONE-02',
            extra={'chassis_id': 99999, 'device_ip': '192.0.2.32'},
        )
        self.assertTrue(node_is_collectable(node))
        from connect.metric_collectors import resolve_topology_chassis
        self.assertEqual(resolve_topology_chassis(node).pk, chassis.pk)
