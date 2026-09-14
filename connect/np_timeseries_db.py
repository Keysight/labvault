"""SQLite pragmas for the np_timeseries database (WAL + busy timeout)."""

from __future__ import annotations

import logging

from django.db.backends.signals import connection_created

logger = logging.getLogger(__name__)


def configure_np_timeseries_connection(sender, connection, **kwargs):
    if connection.alias != 'np_timeseries':
        return
    if connection.vendor != 'sqlite':
        return
    with connection.cursor() as cursor:
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA busy_timeout=5000;')
        cursor.execute('PRAGMA synchronous=NORMAL;')
        cursor.execute('PRAGMA temp_store=MEMORY;')
        cursor.execute('PRAGMA cache_size=-262144;')
    logger.debug('np_timeseries SQLite WAL enabled')


def register_np_timeseries_pragmas():
    connection_created.connect(configure_np_timeseries_connection, dispatch_uid='np_timeseries_wal')
