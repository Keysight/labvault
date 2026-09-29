"""Topology metrics collector loop.

Long-running worker (opsd service ``collector``). Each tick calls
``metric_collectors.collect_all_topologies`` for topologies with
``metrics_collection_enabled``, then ``refresh_stale_insights_snapshots``.
``--once`` runs a single tick; ``--interval`` defaults to 60 seconds.
On the customer SKU ``collector_mode`` defaults to idle, which skips live polls
unless an operator sets the runtime setting to live.
See ``docs/development/subsystems/metrics-insights.md``.
"""
from django.core.management.base import BaseCommand
import time
from django.db import close_old_connections

class Command(BaseCommand):
    help = "Topology metrics collector (idle by default on customer SKU)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=int, default=60)

    def handle(self, *args, **options):
        from connect.metric_collectors import collect_all_topologies, collector_mode

        def refresh_pulse():
            try:
                from connect.lab_usage_insights import refresh_stale_insights_snapshots

                refresh_stale_insights_snapshots()
            except Exception as exc:
                close_old_connections()
                self.stderr.write(f"insights snapshot refresh failed: {exc}")

        def tick():
            mode = collector_mode()
            if mode == "idle":
                self.stdout.write("collector idle (no network, no synthetic rows)")
                refresh_pulse()
                return
            if mode == "live":
                try:
                    n = collect_all_topologies()
                    self.stdout.write(f"wrote {n} metric sample(s)")
                except Exception as exc:
                    close_old_connections()
                    self.stderr.write(f"collector tick failed: {exc}")
                refresh_pulse()
        if options["once"]:
            tick()
            return
        while True:
            tick()
            time.sleep(options["interval"])
