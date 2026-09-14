"""
Pre-warm Lab Topology fabric snapshots (Port Fabric + optional Fabric Map).

Usage:
  python manage.py refresh_topology_fabric_cache --topo-id 20
  python manage.py refresh_topology_fabric_cache --topo-id 20 --cold-only
  python manage.py refresh_topology_fabric_cache --all
"""
from django.core.management.base import BaseCommand

from connect.models import LabTopology
from connect.lab_topology_views import refresh_port_fabric_snapshot


class Command(BaseCommand):
    help = 'Build and store LabTopologyFabricSnapshot rows for fast Port Fabric loads'

    def add_arguments(self, parser):
        parser.add_argument('--topo-id', type=int, action='append', dest='topo_ids')
        parser.add_argument('--all', action='store_true', help='All topologies')
        parser.add_argument(
            '--cold-only',
            action='store_true',
            help='Only build live=0 snapshot (fast, no OCS REST)',
        )
        parser.add_argument(
            '--warm-only',
            action='store_true',
            help='Only build live=1 snapshot (includes OCS crossconnects)',
        )

    def handle(self, *args, **options):
        if options['all']:
            ids = list(LabTopology.objects.values_list('pk', flat=True))
        elif options['topo_ids']:
            ids = options['topo_ids']
        else:
            self.stderr.write('Specify --topo-id ID or --all')
            return

        do_cold = not options['warm_only']
        do_warm = not options['cold_only']

        for topo_id in ids:
            topo = LabTopology.objects.filter(pk=topo_id).first()
            if not topo:
                self.stderr.write(f'Skip missing topology {topo_id}')
                continue
            self.stdout.write(f'Topology {topo_id} ({topo.name})')
            if do_cold:
                self.stdout.write('  cold (live=0, no OCS SSH)...')
                refresh_port_fabric_snapshot(
                    topo_id, want_live=False, want_lldp=True, force_refresh=True,
                )
            if do_warm:
                self.stdout.write('  warm (live=1, OCS REST)...')
                refresh_port_fabric_snapshot(
                    topo_id, want_live=True, want_lldp=True, force_refresh=False,
                )
        self.stdout.write(self.style.SUCCESS('Done.'))
