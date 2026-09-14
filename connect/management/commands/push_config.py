"""
Django management command to push configuration commands to devices.

Usage:
    python manage.py push_config --ip 192.0.2.20 --commands "ntp server 10.0.0.1" "logging host 10.0.0.2"
    python manage.py push_config --vendor arista --commands "lldp run"
    python manage.py push_config --ip 192.0.2.20 --file config_snippet.txt
    python manage.py push_config --ip 192.0.2.20 --commands "ntp server 10.0.0.1" --no-commit
"""
from django.core.management.base import BaseCommand
from connect.models import Device
from connect.views import get_driver


class Command(BaseCommand):
    help = 'Push configuration commands to devices via vendor-specific APIs'

    def add_arguments(self, parser):
        parser.add_argument('--ip', type=str, help='Target a specific device by IP')
        parser.add_argument('--vendor', type=str, help='Target devices by vendor (arista, sonic, fortigate, paloalto)')
        parser.add_argument('--commands', nargs='+', type=str, help='Configuration commands to push')
        parser.add_argument('--file', type=str, help='File containing config commands (one per line)')
        parser.add_argument('--no-commit', action='store_true', help='Do not commit/save after pushing')
        parser.add_argument('--dry-run', action='store_true', help='Show what would be done')

    def handle(self, *args, **options):
        ip = options.get('ip')
        vendor = options.get('vendor')
        commands = options.get('commands') or []
        config_file = options.get('file')
        no_commit = options.get('no_commit', False)
        dry_run = options.get('dry_run', False)

        if config_file:
            try:
                with open(config_file, 'r') as f:
                    commands = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            except FileNotFoundError:
                self.stderr.write(self.style.ERROR(f'File not found: {config_file}'))
                return

        if not commands:
            self.stderr.write(self.style.ERROR('No commands specified. Use --commands or --file'))
            return

        devices = Device.objects.filter(status__in=('online', 'unknown'))
        if ip:
            devices = devices.filter(ip_address=ip)
        if vendor:
            devices = devices.filter(vendor_type=vendor)

        if not devices.exists():
            self.stderr.write(self.style.ERROR('No matching online devices found.'))
            return

        self.stdout.write(self.style.SUCCESS(f'Pushing {len(commands)} command(s) to {devices.count()} device(s)'))
        self.stdout.write(f'  Commands: {commands[:5]}{"..." if len(commands) > 5 else ""}')
        self.stdout.write(f'  Commit: {"No" if no_commit else "Yes"}')
        self.stdout.write('')

        for device in devices:
            label = f'{device.ip_address} ({device.vendor_type}) - {device.hostname or "?"}'
            self.stdout.write(self.style.HTTP_INFO(f'  {label}'))

            if dry_run:
                self.stdout.write(f'    [DRY RUN] Would send {len(commands)} commands')
                continue

            try:
                driver = get_driver(device)
                result = driver.send_config(commands, commit=not no_commit)
                if result.success:
                    data = result.data or {}
                    self.stdout.write(self.style.SUCCESS(f'    OK - {data.get("commands_sent", 0)} commands sent'))
                    for output in data.get('outputs', [])[:5]:
                        self.stdout.write(f'      {output}')
                else:
                    self.stdout.write(self.style.ERROR(f'    FAILED: {result.error}'))
                    for err in (result.data or {}).get('errors', [])[:3]:
                        self.stdout.write(self.style.ERROR(f'      {err}'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'    ERROR: {e}'))
