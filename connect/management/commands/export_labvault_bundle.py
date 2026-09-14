"""Export a portable LabVault multibundle (.tar.gz) for backup or migration."""

from django.core.management.base import BaseCommand

from connect.labvault_bundle import write_bundle


class Command(BaseCommand):
    help = (
        'Write a labvault-multibundle tarball: inventory JSON, topology v3 files, '
        'site schemas, optional LLDP cache / SQLite DBs / secrets.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--output', '-o', required=True,
            help='Output path, e.g. /tmp/ocs-lab-bundle.tar.gz',
        )
        parser.add_argument(
            '--lab', default='',
            help='Scope inventory to a Keysight lab name (e.g. "OCS Lab").',
        )
        parser.add_argument(
            '--ip-prefix', default='',
            help='Scope inventory to devices/chassis with this IPv4 prefix.',
        )
        parser.add_argument(
            '--topology-id', action='append', type=int, dest='topology_ids',
            help='Include topology by primary key (repeatable).',
        )
        parser.add_argument(
            '--topology-name', action='append', dest='topology_names',
            help='Include topology whose name contains this string (repeatable).',
        )
        parser.add_argument(
            '--resource', action='append', dest='resource_files',
            help='Extra resource JSON path relative to project root (repeatable).',
        )
        parser.add_argument(
            '--include-databases', action='store_true',
            help='Embed db.sqlite3 and np_timeseries.sqlite3.',
        )
        parser.add_argument(
            '--include-lldp-cache', action='store_true', default=True,
            help='Embed data/lldp_persistent_cache.json (default: on).',
        )
        parser.add_argument(
            '--no-lldp-cache', action='store_false', dest='include_lldp_cache',
            help='Skip LLDP persistent cache.',
        )
        parser.add_argument(
            '--include-secrets', action='store_true',
            help='Embed secrets/ directory (off by default).',
        )
        parser.add_argument(
            '--include-capex', action='store_true',
            help='Ignored on customer SKU (Capex is hard-dumped).',
        )

    def handle(self, *args, **options):
        lab = (options.get('lab') or '').strip()
        topology_ids = options.get('topology_ids') or []
        topology_names = list(options.get('topology_names') or [])

        # No baked lab name. Without an explicit topology filter, export matching inventory.

        manifest = write_bundle(
            options['output'],
            lab=lab,
            ip_prefix=(options.get('ip_prefix') or '').strip(),
            topology_ids=topology_ids or None,
            topology_names=topology_names or None,
            resource_files=options.get('resource_files') or None,
            include_databases=options['include_databases'],
            include_lldp_cache=options['include_lldp_cache'],
            include_secrets=options['include_secrets'],
            include_capex=options['include_capex'],
        )
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {options['output']}"
        ))
        self.stdout.write(f"Files: {len(manifest.get('files', []))}")
        self.stdout.write(f"Counts: {manifest.get('counts', {})}")
        if options['include_secrets']:
            self.stdout.write(self.style.WARNING(
                'Bundle contains secrets/ — store and transfer securely.'
            ))
