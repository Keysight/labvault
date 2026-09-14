"""APS 1.0 / 1.5 detection from node names and FRU dumps."""

from django.test import SimpleTestCase

from connect.keysight_aps_generations import (
    aps_gen_from_fru,
    is_aps_15_node_name,
    node_aps_generation,
)


class ApsGenerationTests(SimpleTestCase):
    def test_o15_node_name(self):
        self.assertTrue(is_aps_15_node_name('cn-aps-o15-tw20230139'))
        self.assertEqual(
            node_aps_generation('cn-aps-o15-tw20230139'),
            '15',
        )

    def test_o2_node_name(self):
        self.assertTrue(is_aps_15_node_name('cn-aps-o2-sg25341005'))
        self.assertEqual(node_aps_generation('cn-aps-o2-sg25341005'), '15')

    def test_fru_one_150(self):
        self.assertEqual(aps_gen_from_fru('APS-ONE-150', ''), '15')

    def test_fru_one_100(self):
        self.assertEqual(aps_gen_from_fru('APS-ONE-100', ''), '10')

    def test_fru_overrides_generic_cn_name(self):
        self.assertEqual(
            node_aps_generation(
                'cn-aps-tw20230139',
                fru_board_product='APS-ONE-150',
            ),
            '15',
        )

    def test_aps_gen_hint(self):
        self.assertEqual(
            node_aps_generation('cn-aps-unknown', aps_gen_hint='15'),
            '15',
        )
