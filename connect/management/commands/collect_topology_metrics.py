"""Collect lab topology metrics into np_timeseries (daemon or one-shot)."""

from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from connect.metric_collectors import (
    MAX_COLLECT_WORKERS,
    CollectorState,
    collect_all_topologies,
    collector_mode,
    collector_topology_allowlist,
)

logger = logging.getLogger('labvault.collect_topology_metrics')


class Command(BaseCommand):
    help = 'Collect CPU/mem/traffic and OCS patch events for lab topologies (5-minute cadence).'

    def add_arguments(self, parser):
        parser.add_argument('--topology-id', type=int, default=None, help='Limit to one topology')
        parser.add_argument('--daemon', action='store_true', help='Run forever')
        parser.add_argument('--interval', type=int, default=300, help='Seconds between runs (default 300)')
        parser.add_argument('--workers', type=int, default=MAX_COLLECT_WORKERS, help='Parallel collector workers')

    def handle(self, *args, **options):
        topo_id = options.get('topology_id')
        daemon = options.get('daemon')
        interval = max(30, int(options.get('interval') or 300))
        workers = max(1, int(options.get('workers') or MAX_COLLECT_WORKERS))
        state = CollectorState()
        state.load_if_counters()

        if daemon:
            self.stdout.write(
                f'Daemon mode={collector_mode()} interval={interval}s workers={workers} '
                f'topology_id={topo_id or "all"} allowlist={collector_topology_allowlist() or "all"}'
            )
            while True:
                try:
                    n = self._run_once(topo_id, state, workers=workers)
                    state.persist_if_counters()
                    self.stdout.write(f'[{timezone.now():%H:%M:%S}] wrote {n} metric sample(s)')
                except Exception as e:
                    close_old_connections()
                    logger.exception('collect_topology_metrics loop failed: %s', e)
                    self.stderr.write(self.style.ERROR(str(e)))
                time.sleep(interval)
        else:
            n = self._run_once(topo_id, state, workers=workers)
            state.persist_if_counters()
            self.stdout.write(self.style.SUCCESS(f'Wrote {n} metric sample(s)'))

    def _run_once(self, topo_id: int | None, state: CollectorState, *, workers: int) -> int:
        n = collect_all_topologies(topo_id, state=state, workers=workers)
        try:
            from connect.lab_usage_insights import refresh_stale_insights_snapshots

            refresh_stale_insights_snapshots(topo_id)
        except Exception:
            logger.exception('insights snapshot refresh failed')
        return n
