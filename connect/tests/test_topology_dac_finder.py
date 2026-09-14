"""Tests for transceiver-serial DAC/B2B discovery."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from connect.models import LabTopology, LabTopologyLink, LabTopologyNode
from connect.topology_dac_finder import (
    apply_serial_links_to_topology,
    find_serial_pairs,
    normalize_serial,
)


class TestTopologyDacFinder(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('dacfinder', password='test')
        self.topo = LabTopology.objects.create(name='Serial Test', created_by=self.user)
        self.na = LabTopologyNode.objects.create(
            topology=self.topo, label='Chassis A', node_type='chassis',
            x=0, y=0, extra={'chassis_id': 1, 'port_details': [
                {'name': 'port_1', 'serial': 'SN-ABC-001'},
                {'name': 'port_2', 'serial': 'SN-ABC-001'},
            ]},
        )
        self.nb = LabTopologyNode.objects.create(
            topology=self.topo, label='Switch 1', node_type='switch',
            x=200, y=0, extra={'port_details': [
                {'name': 'port_49', 'serial': 'SN-DAC-777'},
            ]},
        )
        self.nc = LabTopologyNode.objects.create(
            topology=self.topo, label='Chassis B', node_type='chassis',
            x=400, y=0, extra={'chassis_id': 2, 'port_details': [
                {'name': 'port_3', 'serial': 'SN-DAC-777'},
            ]},
        )

    def test_normalize_serial_skips_empty(self):
        self.assertEqual(normalize_serial(''), '')
        self.assertEqual(normalize_serial('n/a'), '')
        self.assertEqual(normalize_serial('  sn-1  '), 'SN-1')

    def test_b2b_same_chassis_serial_pair(self):
        entries = [
            {'node_id': self.na.pk, 'port': 'port_1', 'serial': 'SN-ABC-001',
             'node_type': 'chassis', 'chassis_id': 1, 'label': 'A'},
            {'node_id': self.na.pk, 'port': 'port_2', 'serial': 'SN-ABC-001',
             'node_type': 'chassis', 'chassis_id': 1, 'label': 'A'},
        ]
        pairs = find_serial_pairs(entries)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['link_kind'], 'b2b')
        self.assertEqual(pairs[0]['cable_type'], 'direct')

    def test_dac_cross_device_serial_pair(self):
        entries = [
            {'node_id': self.nb.pk, 'port': 'port_49', 'serial': 'SN-DAC-777',
             'node_type': 'switch', 'label': 'Sw'},
            {'node_id': self.nc.pk, 'port': 'port_3', 'serial': 'SN-DAC-777',
             'node_type': 'chassis', 'chassis_id': 2, 'label': 'B'},
        ]
        pairs = find_serial_pairs(entries)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['link_kind'], 'dac')

    def test_apply_creates_links(self):
        pairs = find_serial_pairs([
            {'node_id': self.nb.pk, 'port': 'port_49', 'serial': 'SN-DAC-777', 'node_type': 'switch'},
            {'node_id': self.nc.pk, 'port': 'port_3', 'serial': 'SN-DAC-777', 'node_type': 'chassis'},
        ])
        stats = apply_serial_links_to_topology(self.topo, pairs)
        self.assertEqual(stats['created'], 1)
        self.assertEqual(LabTopologyLink.objects.filter(topology=self.topo).count(), 1)
        lk = LabTopologyLink.objects.get(topology=self.topo)
        self.assertEqual(lk.cable_type, 'dac')
