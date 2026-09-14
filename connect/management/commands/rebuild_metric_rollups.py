"""Rebuild hourly LabMetricRollup rows from raw LabMetricSample data."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from connect.lab_metrics import rebuild_rollups_for_topology
from connect.models import LabTopology


class Command(BaseCommand):
    help = 'Rebuild hourly metric rollups from raw samples (backfill / repair).'

    def add_arguments(self, parser):
        parser.add_argument('--topology-id', type=int, default=None, help='Limit to one topology')

    def handle(self, *args, **options):
        topo_id = options.get('topology_id')
        topo_qs = LabTopology.objects.all()
        if topo_id:
            topo_qs = topo_qs.filter(pk=topo_id)
        total = 0
        for topo in topo_qs:
            n = rebuild_rollups_for_topology(topo.pk)
            total += n
            self.stdout.write(f'topo={topo.pk}: {n} rollup bucket(s) updated')
        self.stdout.write(self.style.SUCCESS(f'Done: {total} rollup bucket(s) total'))
