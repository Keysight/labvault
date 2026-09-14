"""Per-node hardware error flag helpers."""

from django.test import SimpleTestCase, TestCase

from connect.keysight_hw_errors import (
    apply_hw_flags_to_cards,
    count_hw_error_units,
    lookup_node_hw_flag,
)
from connect.models import KeysightBmcEndpoint, KeysightChassis


class LookupNodeHwFlagTests(SimpleTestCase):
    def test_by_node_name(self):
        flags = {'cn-aps-o15-tw1': {'hardware_error_reported': True, 'hardware_error_notes': 'no qat'}}
        hw = lookup_node_hw_flag(flags, node_name='cn-aps-o15-tw1')
        self.assertTrue(hw['hardware_error_reported'])
        self.assertEqual(hw['hardware_error_notes'], 'no qat')


class ApplyHwFlagsToCardsTests(TestCase):
    def test_merges_bmc_flags_onto_cards(self):
        ch = KeysightChassis.objects.create(
            ip_address='10.1.1.1', username='u', password='p', chassis_type='aps_m1010',
        )
        KeysightBmcEndpoint.objects.create(
            hostname='bmc-cn1.example.com',
            chassis=ch,
            node_name='cn-aps-o15-tw1',
            hardware_error_reported=True,
            hardware_error_notes='missing QAT',
        )
        cards = apply_hw_flags_to_cards([
            {'node_name': 'cn-aps-o15-tw1', 'card_number': 5, 'bmc_name': 'bmc-cn1.example.com'},
        ], ch.id)
        self.assertTrue(cards[0]['hardware_error_reported'])
        self.assertEqual(cards[0]['hardware_error_notes'], 'missing QAT')


class CountHwErrorUnitsTests(TestCase):
    def test_counts_flagged_cns(self):
        ch = KeysightChassis.objects.create(
            ip_address='10.1.1.2', username='u', password='p', chassis_type='aps_m1010',
        )
        assoc = {
            ch.id: {
                'compute_nodes': [
                    {'name': 'cn1', 'hardware_error_reported': True, 'aps_gen': '15'},
                    {'name': 'cn2', 'hardware_error_reported': False},
                ],
                'mgmt_node': {'name': 'mgmt', 'hardware_error_reported': False},
            },
        }
        self.assertEqual(count_hw_error_units([ch], assoc), 1)


class BuildSelectionHwSummaryTests(TestCase):
    def test_chassis_bad_only_for_chassis_or_mgmt_flag(self):
        from connect.keysight_hw_errors import build_selection_hw_summary

        ch = KeysightChassis.objects.create(
            ip_address='10.1.1.3', username='u', password='p', chassis_type='aps_m1010',
        )
        assoc = {
            ch.id: {
                'compute_nodes': [
                    {
                        'name': 'cn-aps-o15-tw1',
                        'hardware_error_reported': True,
                        'aps_gen': '15',
                    },
                ],
                'mgmt_node': {'name': 'mgmt', 'hardware_error_reported': False},
            },
        }
        summary = build_selection_hw_summary([ch], assoc)
        self.assertEqual(summary['chassis_bad'], 0)
        self.assertEqual(summary['cn15_bad'], 1)

    def test_chassis_bad_includes_mgmt_flag(self):
        from connect.keysight_hw_errors import build_selection_hw_summary

        ch = KeysightChassis.objects.create(
            ip_address='10.1.1.4', username='u', password='p', chassis_type='aps_m1010',
        )
        assoc = {
            ch.id: {
                'compute_nodes': [],
                'mgmt_node': {'name': 'mgmt', 'hardware_error_reported': True},
            },
        }
        summary = build_selection_hw_summary([ch], assoc)
        self.assertEqual(summary['chassis_bad'], 1)

    def test_chassis_bad_includes_edit_chassis_flag(self):
        from connect.keysight_hw_errors import build_selection_hw_summary

        ch = KeysightChassis.objects.create(
            ip_address='10.1.1.5', username='u', password='p',
            chassis_type='aps_m1010', hardware_error_reported=True,
        )
        assoc = {ch.id: {'compute_nodes': [], 'mgmt_node': {'name': 'mgmt'}}}
        summary = build_selection_hw_summary([ch], assoc)
        self.assertEqual(summary['chassis_bad'], 1)
