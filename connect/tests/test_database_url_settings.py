"""Compose must not route np_timeseries onto the inventory DATABASE_URL."""

import os
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from connect.settings import _database_from_url


class DatabaseUrlRoutingTests(SimpleTestCase):
    def test_timeseries_url_is_not_swallowed_by_database_url(self):
        env = {
            'DATABASE_URL': 'postgres://labvault:labvault@db:5432/labvault',
            'NP_TIMESERIES_DATABASE_URL': (
                'postgres://labvault:labvault@metrics-db:5432/labvault_metrics'
            ),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            default = _database_from_url('DATABASE_URL', Path('/tmp/db.sqlite3'))
            ts = _database_from_url(
                'NP_TIMESERIES_DATABASE_URL', Path('/tmp/np_timeseries.sqlite3')
            )
        self.assertEqual(default.get('HOST'), 'db')
        self.assertEqual(default.get('NAME'), 'labvault')
        self.assertEqual(ts.get('HOST'), 'metrics-db')
        self.assertEqual(ts.get('NAME'), 'labvault_metrics')
