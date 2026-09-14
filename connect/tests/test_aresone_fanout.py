from django.test import SimpleTestCase

from connect.aresone_fanout import (
    plan_aresone_requirement,
    validate_lab_aresone_capacity,
)


class AresoneFanoutTests(SimpleTestCase):
    def test_800g_two_ports_one_pair_eight_rgs_one_card(self):
        plan = plan_aresone_requirement(2, "800G")
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["fanout_mode"], "8x1x800G")
        self.assertEqual(plan["pairs_needed"], 1)
        self.assertEqual(plan["rgs_needed"], 2)
        self.assertEqual(plan["cards_needed"], 1)

    def test_100g_sixteen_ports(self):
        plan = plan_aresone_requirement(16, "100G")
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["fanout_mode"], "8x8x100G")
        self.assertEqual(plan["pairs_needed"], 8)
        self.assertEqual(plan["rgs_needed"], 2)

    def test_unsupported_rate(self):
        plan = plan_aresone_requirement(2, "400G")
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["error"], "unsupported_line_rate")

    def test_lab_validation_with_fabric(self):
        fabric = {
            "nodes_by_kind": {"chassis": 2, "ocs": 1},
            "ocs_paths": {"total": 4, "active": 4},
            "chassis": [
                {
                    "label": "ARESONE-M01",
                    "family": "aresone",
                    "slots": [
                        {
                            "slot": 1,
                            "card_type": "800GE-8P-OSFP-M+NRZ+ROCEV2",
                            "mode": "800G",
                            "ports_up": 6,
                            "ports_total": 8,
                        }
                    ],
                },
                {
                    "label": "ARESONE-M02",
                    "family": "aresone",
                    "slots": [
                        {
                            "slot": 1,
                            "card_type": "800GE-8P-OSFP-M+NRZ+ROCEV2",
                            "mode": "800G",
                            "ports_up": 8,
                            "ports_total": 8,
                        }
                    ],
                },
            ],
        }
        v = validate_lab_aresone_capacity(fabric, 2, "800G", via_ocs="required")
        self.assertTrue(v["ok"])
        self.assertTrue(v["ports_sufficient"])
        self.assertTrue(v["rg_sufficient"])
        self.assertEqual(v["ports_up"], 14)
        self.assertEqual(v["ports_total"], 16)
        self.assertEqual(v["rg_capacity"], 16)

    def test_100g_logical_pool_from_800g_slots(self):
        """800GE cards in 800G phy mode still expose 8×8×100G logical pool."""
        fabric = {
            "nodes_by_kind": {"chassis": 2},
            "ocs_paths": {"total": 0, "active": 0},
            "chassis": [
                {
                    "label": "ARESONE-M01",
                    "family": "aresone",
                    "slots": [
                        {
                            "slot": 1,
                            "card_type": "800GE-8P-OSFP-M+NRZ+ROCEV2",
                            "mode": "800G",
                            "ports_up": 16,
                            "ports_total": 16,
                        }
                    ],
                },
                {
                    "label": "ARESONE-M02",
                    "family": "aresone",
                    "slots": [
                        {
                            "slot": 1,
                            "card_type": "800GE-8P-OSFP-M+NRZ+ROCEV2",
                            "mode": "800G",
                            "ports_up": 14,
                            "ports_total": 16,
                        }
                    ],
                },
            ],
        }
        v = validate_lab_aresone_capacity(fabric, 8, "100G", via_ocs="none")
        self.assertTrue(v["ok"])
        self.assertTrue(v["ports_sufficient"])
        self.assertEqual(v["fanout_mode"], "8x8x100G")
        self.assertEqual(v["ports_up"], 64 + 56)
        self.assertEqual(v["ports_total"], 128)
        self.assertEqual(v["rg_capacity"], 16)
