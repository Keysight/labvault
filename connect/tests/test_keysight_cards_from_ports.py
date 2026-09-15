"""Fleet port cache can render chassis cards without a blocking IxOS get_cards."""
from django.test import SimpleTestCase

from connect.keysight_port_cache import (
    cache_has_live_ixos_cards,
    cards_from_cached_ports,
    hydrate_cards_from_port_cache,
)


class CardsFromPortCacheTests(SimpleTestCase):
    def test_groups_ports_by_card(self):
        ports = [
            {'card_number': 1, 'port_number': 9, 'link_state': 'up', 'owner': 'Free'},
            {'card_number': 1, 'port_number': 10, 'link_state': 'down', 'owner': 'qa'},
            {'card_number': 2, 'port_number': 1, 'link_state': 'up', 'owner': 'Free'},
        ]
        cards = cards_from_cached_ports(ports)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]['card_number'], 1)
        self.assertEqual(cards[0]['ports_total'], 2)
        self.assertEqual(cards[0]['ports_up'], 1)
        self.assertEqual(cards[0]['ports_owned'], 1)
        self.assertTrue(cards[0]['resource_groups'])

    def test_hydrate_skips_when_cards_already_present(self):
        cached = hydrate_cards_from_port_cache({
            'cards': [{'card_number': 1, 'type': 'live'}],
            'ports': [{'card_number': 1, 'port_number': 1}],
        })
        self.assertEqual(cached['cards'][0]['type'], 'live')
        self.assertFalse(cached.get('_cards_from_ports'))

    def test_hydrate_fills_empty_cards(self):
        cached = hydrate_cards_from_port_cache({
            'cards': [],
            'ports': [{'card_number': 1, 'port_number': 1, 'link_state': 'up', 'owner': 'Free'}],
        })
        self.assertTrue(cached['_cards_from_ports'])
        self.assertEqual(len(cached['cards']), 1)
        self.assertEqual(cached['total_ports'], 1)

    def test_live_ixos_cards_are_not_treated_as_fleet_fallback(self):
        self.assertTrue(cache_has_live_ixos_cards({
            'cards': [{'card_number': 1, 'type': 'Novus-100G'}],
        }))
        self.assertFalse(cache_has_live_ixos_cards({
            '_cards_from_ports': True,
            'cards': [{'card_number': 1, '_from_fleet_ports': True}],
        }))
        self.assertFalse(cache_has_live_ixos_cards({'cards': []}))
