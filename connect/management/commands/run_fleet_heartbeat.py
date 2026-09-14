"""Run the fleet heartbeat worker (Google demo Oncaller path)."""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from connect.fleet_heartbeat import heartbeat_interval_seconds, run_heartbeat_tick


class Command(BaseCommand):
    help = 'Poll AresONE/Keysight chassis heartbeats and update shared cache.'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=int, default=0, help='Seconds between ticks (default env)')
        parser.add_argument('--once', action='store_true', help='Run a single tick and exit')

    def handle(self, *args, **options):
        interval = options['interval'] or heartbeat_interval_seconds()
        if options['once']:
            payload = run_heartbeat_tick()
            self.stdout.write(self.style.SUCCESS(
                f"tick ok mode={payload.get('mode')} chassis={payload.get('counts', {}).get('total')}"
            ))
            return
        self.stdout.write(f'Starting fleet heartbeat worker interval={interval}s')
        while True:
            try:
                payload = run_heartbeat_tick()
                counts = payload.get('counts') or {}
                self.stdout.write(
                    f"heartbeat tick mode={payload.get('mode')} "
                    f"ok={counts.get('heartbeat_ok')}/{counts.get('total')} "
                    f"halt={counts.get('halt_suspect')}"
                )
            except Exception as exc:
                # Recover stale DB handles after postgres restarts; do not close
                # connections on every successful tick (single-threaded loop).
                close_old_connections()
                self.stderr.write(f'heartbeat tick failed: {exc}')
            time.sleep(interval)