"""Idempotent first-deploy bootstrap for Topology Insights (Lab Pulse).

Guarantees the pieces Insights needs so a fresh install never "looks healthy"
while the feature is silently dead:

1. Migrate the dedicated ``np_timeseries`` database (creates LabMetricSample etc.).
2. Verify the metric table is queryable (hard-fail with ``--check-only``).
3. Disable metrics collection on topologies with no collectable nodes so the
   collector never hangs polling unreachable imported-LLDP graphs.
4. In seeded mode (``LABVAULT_COLLECTOR_MODE=seeded``) backfill a short synthetic
   history so demo charts are populated immediately.

Safe to re-run. Wired into customer oneshots / labvaultctl install, and
deploy/scripts/production-merge-deploy.sh.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from connect.lab_metrics import TS_DB
from connect.metric_collectors import (
    collector_mode,
    seed_topology_metrics,
    topology_has_collectable_nodes,
)
from connect.models import LabMetricSample, LabTopology


class Command(BaseCommand):
    help = 'Ensure Topology Insights storage is migrated, seeded, and collectable.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--check-only', action='store_true',
            help='Do not migrate/seed; verify readiness and exit non-zero if not ready.',
        )
        parser.add_argument(
            '--no-seed', action='store_true',
            help='Skip synthetic backfill even in seeded mode (schema only).',
        )
        parser.add_argument(
            '--no-bootstrap-disable', action='store_true',
            help='Do not auto-disable topologies that have no collectable nodes.',
        )
        parser.add_argument(
            '--backfill-points', type=int, default=24,
            help='Seeded-mode history points to backfill per topology (default 24).',
        )

    def _table_ready(self) -> bool:
        try:
            LabMetricSample.objects.using(TS_DB).exists()
            return True
        except Exception:  # noqa: BLE001 — OperationalError etc. => not migrated
            return False

    def handle(self, *args, **options):
        check_only = options['check_only']
        mode = collector_mode()

        if check_only:
            if not self._table_ready():
                raise CommandError(
                    'np_timeseries not migrated — run: manage.py ensure_topology_insights'
                )
            enabled = LabTopology.objects.filter(metrics_collection_enabled=True).count()
            self.stdout.write(self.style.SUCCESS(
                f'Topology Insights ready: np_timeseries OK, mode={mode}, '
                f'topologies_enabled={enabled}'
            ))
            return

        # 1. Migrate np_timeseries (router restricts it to NP time-series models).
        self.stdout.write('Migrating np_timeseries database...')
        call_command('migrate', database=TS_DB, interactive=False, verbosity=0)

        # 2. Verify the metric table now exists.
        if not self._table_ready():
            raise CommandError('np_timeseries migration did not create LabMetricSample table')

        # 3. Bootstrap: disable topologies with no collectable nodes.
        disabled = []
        if not options['no_bootstrap_disable']:
            for topo in LabTopology.objects.filter(metrics_collection_enabled=True):
                if not topology_has_collectable_nodes(topo.pk):
                    topo.metrics_collection_enabled = False
                    topo.save(update_fields=['metrics_collection_enabled', 'updated_at'])
                    disabled.append(topo.name)

        # 4. Seed synthetic history in demo/seeded mode. Idempotent: skip a
        # topology that already has recent samples (collector keeps it fresh),
        # so web restarts do not accumulate duplicate backfill history.
        seeded_rows = 0
        seeded_topos = 0
        if mode == 'seeded' and not options['no_seed']:
            recent_cut = timezone.now() - timedelta(minutes=30)
            for topo in LabTopology.objects.filter(metrics_collection_enabled=True):
                has_recent = LabMetricSample.objects.using(TS_DB).filter(
                    topology_id=topo.pk, sampled_at__gte=recent_cut,
                ).exists()
                if has_recent:
                    continue
                wrote = seed_topology_metrics(
                    topo.pk, backfill_points=options['backfill_points'],
                )
                if wrote:
                    seeded_rows += wrote
                    seeded_topos += 1

        enabled = LabTopology.objects.filter(metrics_collection_enabled=True).count()
        if disabled:
            self.stdout.write(
                f'Disabled metrics on {len(disabled)} topology(ies) with no collectable '
                f'nodes: {", ".join(disabled[:10])}'
                + (' ...' if len(disabled) > 10 else '')
            )
        if seeded_rows:
            self.stdout.write(
                f'Seeded {seeded_rows} synthetic sample(s) across {seeded_topos} topology(ies).'
            )
        self.stdout.write(self.style.SUCCESS(
            f'Topology Insights ready: np_timeseries migrated, mode={mode}, '
            f'topologies_enabled={enabled}'
        ))
