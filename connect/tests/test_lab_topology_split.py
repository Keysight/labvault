"""Tests for HBG OCS sub-topology split."""
from django.test import TestCase

from connect.lab_topology_split import (
    WITH_OCS_NODE_KEYS,
    WITHOUT_OCS_NODE_KEYS,
    _filter_export_payload,
    _strip_ocs_from_node_extra,
)


class LabTopologySplitTests(TestCase):
    def test_filter_without_ocs_strips_ocs_ports(self):
        payload = {
            'topology': {'extra': {}},
            'nodes': [
                {'node_key': 'aresone01', 'node_type': 'chassis', 'extra': {
                    'port_details': [
                        {'name': 'port_1', 'role': 'ocs'},
                        {'name': '1.1', 'role': 'dac'},
                    ],
                    'ports': ['port_1', '1.1'],
                }},
                {'node_key': 'ocs', 'node_type': 'ocs', 'extra': {}},
                {'node_key': 'aresone05', 'node_type': 'chassis', 'extra': {}},
            ],
            'links': [
                {'node_a': 'aresone01', 'node_b': 'arista1', 'cable_type': 'dac'},
                {'node_a': 'aresone05', 'node_b': 'ocs', 'cable_type': 'optic'},
            ],
        }
        out = _filter_export_payload(
            payload,
            keep_keys=WITHOUT_OCS_NODE_KEYS,
            strip_ocs_ports=True,
            sub_role='without_ocs_patch',
        )
        keys = {n['node_key'] for n in out['nodes']}
        self.assertIn('aresone01', keys)
        self.assertNotIn('ocs', keys)
        self.assertNotIn('aresone05', keys)
        self.assertEqual(len(out['links']), 0)
        ex = out['nodes'][0]['extra']
        self.assertFalse(ex.get('ocs_stage_enabled'))
        names = [p['name'] for p in ex.get('port_details') or []]
        self.assertEqual(names, ['1.1'])

    def test_strip_ocs_from_node_extra(self):
        ex = _strip_ocs_from_node_extra({
            'port_details': [{'name': 'port_2', 'role': 'ocs'}],
            'ports': ['port_2', '2.1'],
            'ocs_physical_ports': [{'x': 1}],
        })
        self.assertEqual(ex.get('ports'), ['2.1'])
        self.assertNotIn('ocs_physical_ports', ex)

    def test_with_ocs_keeps_ocs_node(self):
        payload = {
            'topology': {'extra': {}},
            'nodes': [
                {'node_key': 'ocs', 'node_type': 'ocs', 'extra': {}},
                {'node_key': 'aresone05', 'node_type': 'chassis', 'extra': {}},
            ],
            'links': [],
        }
        out = _filter_export_payload(
            payload,
            keep_keys=WITH_OCS_NODE_KEYS,
            strip_ocs_ports=False,
            sub_role='with_ocs_patch',
        )
        keys = {n['node_key'] for n in out['nodes']}
        self.assertIn('ocs', keys)
        self.assertIn('aresone05', keys)
