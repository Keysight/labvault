"""
Split HBG datacenter topology into DAC-staging and OCS-patched sub-topologies.

  python manage.py split_hbg_sub_topologies --topo-id 25
  python manage.py split_hbg_sub_topologies --topo-id 25 --enrich --fabric-cache
"""
from django.core.management.base import BaseCommand, CommandError

from connect.lab_topology_split import split_hbg_ocs_sub_topologies
from connect.models import LabTopology


class Command(BaseCommand):
    help = 'Create without-OCS and with-OCS sub-topologies from an HBG parent topology.'

    def add_arguments(self, parser):
        parser.add_argument('--topo-id', type=int, required=True)
        parser.add_argument(
            '--keep-existing',
            action='store_true',
            help='Do not delete prior sub-topologies listed on the parent',
        )
        parser.add_argument('--enrich', action='store_true', help='Run enrich_topology_ports_from_cache')
        parser.add_argument('--fabric-cache', action='store_true', help='Rebuild port-fabric cold snapshots')

    def handle(self, *args, **options):
        parent = LabTopology.objects.filter(pk=options['topo_id']).first()
        if not parent:
            raise CommandError(f'Topology {options["topo_id"]} not found')

        result = split_hbg_ocs_sub_topologies(
            parent,
            replace_existing=not options['keep_existing'],
        )
        for role, topo in result.items():
            self.stdout.write(f'  {role}: id={topo.pk} {topo.name} '
                              f'({topo.nodes.count()} nodes, {topo.links.count()} links)')

        if options['enrich']:
            from connect.topology_dac_finder import enrich_topology_ports_from_cache
            for topo in result.values():
                n = enrich_topology_ports_from_cache(topo, refresh=False)
                self.stdout.write(f'  enriched {topo.pk}: {n} nodes')

        if options['fabric_cache']:
            from connect.lab_topology_views import refresh_port_fabric_snapshot
            for topo in result.values():
                refresh_port_fabric_snapshot(
                    topo.pk, want_live=False, want_lldp=True, force_refresh=True,
                )
                self.stdout.write(f'  port-fabric cache: {topo.pk}')

        self.stdout.write(self.style.SUCCESS(
            f'Parent {parent.pk} updated with sub_topologies in extra',
        ))
