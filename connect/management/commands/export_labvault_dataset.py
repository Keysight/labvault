"""Export full LabVault dataset JSON for migration/replication."""

from django.core.management.base import BaseCommand

from connect.labvault_dataset import write_export_file


class Command(BaseCommand):
    help = 'Write a full LabVault JSON export (devices, Keysight, audit, topology).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output', '-o', required=True,
            help='Output path, e.g. /tmp/labvault-export.json',
        )

    def handle(self, *args, **options):
        counts = write_export_file(options['output'])
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {options['output']} — {counts}"
        ))
        self.stdout.write('Copy media/ to the target host if attachments are used.')
