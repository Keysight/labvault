"""
Management command: python manage.py ipmi_discover

Probes a list of IPs for IPMI/Redfish BMC endpoints and adds them to
KeysightBmcEndpoint inventory.
"""
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand
from django.utils import timezone

from connect.models import KeysightBmcEndpoint
from connect.redfish_utils import redfish_is_available

logger = logging.getLogger('labvault.ipmi_discover')

DEFAULT_BMC_USERS = [
    ('admin', 'admin'),
    ('ADMIN', 'ADMIN'),
    ('root', 'root'),
]


def _check_ipmi(ip, username, password, timeout=5):
    """Try ipmitool mc info to check if IPMI is reachable."""
    cmd = [
        'ipmitool', '-H', str(ip), '-U', username, '-P', password,
        '-I', 'lanplus', 'mc', 'info',
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0 and 'Firmware Revision' in proc.stdout:
            return True, proc.stdout
    except Exception:
        pass
    return False, ''


def probe_bmc(ip, credentials=None, timeout=5):
    """
    Probe an IP for BMC endpoints (Redfish first, then IPMI).
    Returns a result dict or None if unreachable.
    """
    creds = credentials or DEFAULT_BMC_USERS

    for username, password in creds:
        if redfish_is_available(str(ip), username, password, timeout=timeout):
            return {
                'ip': str(ip),
                'protocol': 'redfish',
                'username': username,
                'password': password,
                'hostname': '',
                'device_info': 'Redfish BMC',
            }

    for username, password in creds:
        ok, info = _check_ipmi(str(ip), username, password, timeout=timeout)
        if ok:
            hostname = ''
            for line in info.splitlines():
                if 'Product Name' in line:
                    hostname = line.split(':', 1)[-1].strip()
            return {
                'ip': str(ip),
                'protocol': 'ipmi',
                'username': username,
                'password': password,
                'hostname': hostname,
                'device_info': info[:500],
            }

    return None


class Command(BaseCommand):
    help = 'Discover IPMI/Redfish BMC endpoints and add them to Keysight BMC inventory'

    def add_arguments(self, parser):
        parser.add_argument('ips', nargs='*', help='IPs to probe')
        parser.add_argument(
            '--from-existing', action='store_true',
            help='Re-probe IPs already present on KeysightBmcEndpoint rows',
        )
        parser.add_argument('--username', type=str, default='',
                            help='BMC username (overrides defaults)')
        parser.add_argument('--password', type=str, default='',
                            help='BMC password (overrides defaults)')
        parser.add_argument('--workers', type=int, default=10)
        parser.add_argument('--timeout', type=int, default=5)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        ips = list(options['ips'])
        timeout = options['timeout']
        workers = options['workers']
        dry_run = options['dry_run']

        credentials = None
        if options['username'] and options['password']:
            credentials = [(options['username'], options['password'])]

        if options['from_existing']:
            ips.extend(
                KeysightBmcEndpoint.objects.exclude(ip_address='')
                .values_list('ip_address', flat=True)
                .distinct()
            )

        # de-dupe while preserving order
        seen = set()
        unique_ips = []
        for ip in ips:
            ip = str(ip).strip()
            if ip and ip not in seen:
                seen.add(ip)
                unique_ips.append(ip)
        ips = unique_ips

        if not ips:
            self.stdout.write(self.style.WARNING('No IPs to probe'))
            return

        self.stdout.write(f'Probing {len(ips)} IPs for BMC endpoints...')
        found = 0
        created = 0
        updated = 0

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(probe_bmc, ip, credentials, timeout): ip
                for ip in ips
            }
            for future in as_completed(futures):
                ip = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'  {ip}: ERROR - {e}'))
                    continue

                if result is None:
                    self.stdout.write(f'  {ip}: no BMC found')
                    continue

                found += 1
                protocol = result['protocol']
                self.stdout.write(self.style.SUCCESS(
                    f'  {ip}: {protocol} BMC (user={result["username"]})'
                ))

                if dry_run:
                    continue

                hostname = (result.get('hostname') or '').strip() or f'bmc-{result["ip"]}'
                obj, was_created = KeysightBmcEndpoint.objects.update_or_create(
                    hostname=hostname,
                    defaults={
                        'ip_address': result['ip'],
                        'source': 'manual_import',
                        'username': result['username'],
                        'password': result['password'],
                        'last_seen': timezone.now(),
                        'last_error': '',
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nDone: {found} BMC endpoints found, {created} created, {updated} updated'
        ))
