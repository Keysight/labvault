"""Import full LabVault dataset JSON from export_labvault_dataset."""

from django.core.management.base import BaseCommand, CommandError

from connect.labvault_dataset import import_from_file


class Command(BaseCommand):
    help = 'Import a full LabVault JSON export produced by export_labvault_dataset.'

    def add_arguments(self, parser):
        parser.add_argument('input_path', help='Path to labvault-export.json')
        parser.add_argument(
            '--skip-capex',
            action='store_true',
            help='Ignored in this tree.',
        )

    def handle(self, *args, **options):
        try:
            stats = import_from_file(
                options['input_path'],
                import_capex=not options['skip_capex'],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f'Import complete: {stats}'))
