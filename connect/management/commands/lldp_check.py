"""
Management command: python manage.py lldp_check [--ip IP]

Probes devices, fetches LLDP neighbors, and runs topology discovery.
Useful for verifying LLDP mappings between Keysight HW and switches.
"""
from django.core.management.base import BaseCommand

from connect.models import Device
from connect.drivers import get_driver
from connect.topology import discover_topology
from django.utils import timezone


class Command(BaseCommand):
    help = 'Probe devices, fetch LLDP, and run topology discovery'

    def add_arguments(self, parser):
        parser.add_argument('--ip', type=str, help='Check specific device IP only')

    def handle(self, *args, **options):
        ip_filter = options.get('ip')
        devices = Device.objects.exclude(status='maintenance')
        if ip_filter:
            devices = devices.filter(ip_address=ip_filter)
        if not devices.exists():
            self.stderr.write('No devices found')
            return

        self.stdout.write('=== Probing devices ===')
        for d in devices:
            driver = get_driver(d)
            result = driver.probe()
            d.status = 'online' if result == 'ok' else 'offline'
            d.save(update_fields=['status'])
            self.stdout.write(f'  {d.ip_address} ({d.vendor_type}): {result} -> {d.status}')

        self.stdout.write('\n=== LLDP neighbors (online devices) ===')
        for d in devices.filter(status='online'):
            driver = get_driver(d)
            result = driver.get_lldp_neighbors_detail()
            if result.success and result.data:
                self.stdout.write(self.style.SUCCESS(f'  {d.ip_address} ({d.hostname or d.ip_address}): {len(result.data)} neighbors'))
                for n in result.data[:5]:
                    self.stdout.write(f'    {n.get("local_port")} -> {n.get("remote_device")} / {n.get("remote_port")}')
                if len(result.data) > 5:
                    self.stdout.write(f'    ... and {len(result.data) - 5} more')
            else:
                self.stdout.write(f'  {d.ip_address}: {result.error or "no neighbors"}')

        self.stdout.write('\n=== Topology discovery ===')
        data = discover_topology()
        self.stdout.write(f'  Nodes: {len(data.get("nodes", []))}')
        for n in data.get('nodes', []):
            self.stdout.write(f'    {n.get("name")} ({n.get("ip")}) - {n.get("vendor")} - {n.get("status")}')
        self.stdout.write(f'  Links: {len(data.get("links", []))}')
        for l in data.get('links', []):
            self.stdout.write(self.style.SUCCESS(f'    {l.get("local_port")} <-> {l.get("remote_port")}'))
        if data.get('errors'):
            self.stdout.write(self.style.WARNING(f'  Errors: {data["errors"]}'))
