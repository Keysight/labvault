"""
Django management command to enable LLDP on all (or specific) devices.

Usage:
    python manage.py enable_lldp                 # All online devices
    python manage.py enable_lldp --ip 192.0.2.20  # Specific device
    python manage.py enable_lldp --vendor arista   # All Arista devices
    python manage.py enable_lldp --dry-run         # Show what would be done
"""
from django.core.management.base import BaseCommand
from connect.models import Device
from connect.views import get_driver


class Command(BaseCommand):
    help = 'Enable LLDP on all (or specific) devices using vendor-specific APIs'

    def add_arguments(self, parser):
        parser.add_argument('--ip', type=str, help='Target a specific device by IP')
        parser.add_argument('--vendor', type=str, help='Target devices by vendor (arista, sonic, fortigate, paloalto)')
        parser.add_argument('--dry-run', action='store_true', help='Show what would be done without making changes')

    def handle(self, *args, **options):
        ip = options.get('ip')
        vendor = options.get('vendor')
        dry_run = options.get('dry_run', False)

        devices = Device.objects.filter(status__in=('online', 'unknown'))
        if ip:
            devices = devices.filter(ip_address=ip)
        if vendor:
            devices = devices.filter(vendor_type=vendor)

        if not devices.exists():
            self.stderr.write(self.style.ERROR('No matching online devices found.'))
            return

        self.stdout.write(self.style.SUCCESS(f'Found {devices.count()} device(s) to configure'))
        self.stdout.write('')

        results = {}
        for device in devices:
            label = f'{device.ip_address} ({device.vendor_type}) - {device.hostname or "?"}'
            self.stdout.write(self.style.HTTP_INFO(f'  {label}'))

            if dry_run:
                self.stdout.write(f'    [DRY RUN] Would enable LLDP via {device.vendor_type} driver')
                results[device.ip_address] = 'dry-run'
                continue

            try:
                driver = get_driver(device)
                result = driver.enable_lldp()
                if result.success:
                    data = result.data or {}
                    count = data.get('enabled_count', 0)
                    self.stdout.write(self.style.SUCCESS(f'    OK - {count} interfaces'))
                    for detail in data.get('details', [])[:5]:
                        self.stdout.write(f'      {detail}')
                    results[device.ip_address] = f'OK ({count} interfaces)'
                else:
                    self.stdout.write(self.style.ERROR(f'    FAILED: {result.error}'))
                    results[device.ip_address] = f'FAILED: {result.error}'
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'    ERROR: {e}'))
                results[device.ip_address] = f'ERROR: {e}'

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 50))
        self.stdout.write(self.style.SUCCESS('SUMMARY'))
        self.stdout.write(self.style.SUCCESS('=' * 50))
        for ip_addr, status in results.items():
            self.stdout.write(f'  {ip_addr:18s} -> {status}')
