"""
Check / attempt to enable LLDP on a SONiC Device.

  python manage.py sonic_lldp --ip 192.0.2.10

If RESTCONF shows no LLDP and /api/v1/cli is missing (common on newer SONiC),
prints SSH commands to run on the switch.
"""
from django.core.management.base import BaseCommand

from connect.models import Device
from connect.drivers import get_driver


class Command(BaseCommand):
    help = 'Check LLDP on SONiC and enable via REST when possible'

    def add_arguments(self, parser):
        parser.add_argument('--ip', type=str, required=True, help='Switch management IP')

    def handle(self, *args, **options):
        ip = options['ip']
        d = Device.objects.filter(ip_address=ip, vendor_type='sonic').first()
        if not d:
            d = Device.objects.filter(ip_address=ip).first()
        if not d:
            self.stderr.write(self.style.ERROR(f'No Device found for {ip}'))
            return

        drv = get_driver(d)
        if getattr(drv, 'VENDOR_NAME', '') != 'sonic':
            self.stderr.write(self.style.ERROR(f'Device {ip} is not sonic ({d.vendor_type})'))
            return

        self.stdout.write(self.style.SUCCESS(f'=== SONiC LLDP: {ip} ===\n'))

        if hasattr(drv, 'check_lldp_status'):
            st = drv.check_lldp_status()
            self.stdout.write(f"RESTCONF reachable: {st['reachable_restconf']}")
            self.stdout.write(f"LLDP REST root empty: {st['lldp_root_empty']}")
            self.stdout.write(f"LLDP /interfaces usable: {st['interfaces_path_ok']}")
            self.stdout.write(f"Neighbors (REST): {st['neighbor_count']}")
            self.stdout.write(f"/api/v1/cli available: {st['cli_api_ok']}")
            self.stdout.write('')

        lldp = drv.get_lldp_neighbors_detail()
        if lldp.success and lldp.data:
            self.stdout.write(self.style.SUCCESS(f"LLDP neighbors ({len(lldp.data)}):"))
            for n in lldp.data[:20]:
                self.stdout.write(
                    f"  {n.get('local_port')} -> {n.get('remote_device')} / {n.get('remote_port')}"
                )
            if len(lldp.data) > 20:
                self.stdout.write(f'  ... +{len(lldp.data) - 20} more')
            return

        self.stdout.write(self.style.WARNING(f"get_lldp_neighbors_detail: {lldp.error or 'no data'}\n"))

        self.stdout.write('Attempting enable_lldp()...')
        res = drv.enable_lldp()
        for line in (res.data or {}).get('details', []) if res.data else []:
            self.stdout.write(f'  {line}')
        if res.success:
            self.stdout.write(self.style.SUCCESS('\nenable_lldp reported success.'))
        else:
            self.stdout.write(self.style.ERROR(f"\nenable_lldp: {res.error}"))
            ssh = (res.data or {}).get('ssh_steps')
            if ssh:
                self.stdout.write(self.style.WARNING('\nRun on the switch (SSH as a user with sudo):\n'))
                self.stdout.write(ssh + '\n')
