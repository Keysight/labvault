"""Import a LabVault multibundle (.tar.gz) produced by export_labvault_bundle."""

from django.core.management.base import BaseCommand, CommandError

from connect.labvault_bundle import import_bundle


class Command(BaseCommand):
    help = 'Import a labvault-multibundle tarball (inventory, topologies, site, cache, DBs).'

    def add_arguments(self, parser):
        parser.add_argument(
            'bundle', help='Path to .tar.gz multibundle',
        )
        parser.add_argument(
            '--import-capex', action='store_true',
            help='Ignored on customer SKU (Capex is hard-dumped).',
        )
        parser.add_argument(
            '--no-replace-topologies', action='store_true',
            help='Merge into existing topologies instead of replacing nodes/links.',
        )
        parser.add_argument(
            '--import-databases', action='store_true',
            help='Overwrite local SQLite databases from bundle (requires restart).',
        )
        parser.add_argument(
            '--no-lldp-cache', action='store_false', dest='import_lldp_cache',
            default=True,
            help='Skip LLDP persistent cache import.',
        )
        parser.add_argument(
            '--import-secrets', action='store_true',
            help='Copy secrets/ from bundle (off by default).',
        )

    def handle(self, *args, **options):
        try:
            stats = import_bundle(
                options['bundle'],
                import_capex=options['import_capex'],
                replace_topologies=not options['no_replace_topologies'],
                import_databases=options['import_databases'],
                import_lldp_cache=options['import_lldp_cache'],
                import_secrets=options['import_secrets'],
            )
        except (ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Imported {options['bundle']}"))
        if stats.get('inventory'):
            self.stdout.write(f"Inventory: {stats['inventory']}")
        if stats.get('site_import'):
            self.stdout.write(f"Site import: {stats['site_import']}")
        for topo in stats.get('topologies', []):
            self.stdout.write(f"Topology {topo.get('name')} (id={topo.get('id')}): {topo.get('detail')}")
        if stats.get('lldp_cache'):
            self.stdout.write(f"LLDP cache: {stats['lldp_cache']}")
        if stats.get('databases'):
            self.stdout.write(self.style.WARNING(
                f"Databases overwritten: {stats['databases']} — restart app/collector."
            ))
