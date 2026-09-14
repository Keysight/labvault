"""
Enable IxOS chassis LLDP peer-info (``set lldp-peer-info enabled``).

WARNING: applying this restarts IxServer on each target chassis and disconnects
active users/tests on that box.

Usage:
    python manage.py enable_chassis_lldp --ip 192.0.2.10
    python manage.py enable_chassis_lldp --dry-run
    python manage.py enable_chassis_lldp --online --limit 5
"""
from django.core.management.base import BaseCommand

from connect.keysight_drivers import get_driver
from connect.models import KeysightChassis


class Command(BaseCommand):
    help = 'Enable IxOS lldp-peer-info on chassis (restarts IxServer per chassis)'

    def add_arguments(self, parser):
        parser.add_argument('--ip', type=str, help='Target a specific chassis by IP')
        parser.add_argument(
            '--online',
            action='store_true',
            help='All chassis with status=online (use with care)',
        )
        parser.add_argument('--limit', type=int, default=0, help='Max chassis to process (0 = no limit)')
        parser.add_argument('--dry-run', action='store_true', help='Show targets / status only')
        parser.add_argument(
            '--skip-enabled',
            action='store_true',
            default=True,
            help='Skip chassis that already report enabled (default)',
        )
        parser.add_argument(
            '--include-enabled',
            action='store_true',
            help='Also process chassis that already report enabled',
        )

    def handle(self, *args, **options):
        ip = options.get('ip')
        online = options.get('online')
        dry_run = options.get('dry_run', False)
        limit = options.get('limit') or 0
        skip_enabled = not options.get('include_enabled')

        if not ip and not online:
            self.stderr.write(self.style.ERROR('Specify --ip <addr> or --online'))
            return

        qs = KeysightChassis.objects.all().order_by('ip_address')
        if ip:
            qs = qs.filter(ip_address=ip)
        if online:
            qs = qs.filter(status='online')

        chassis = list(qs)
        if limit > 0:
            chassis = chassis[:limit]

        if not chassis:
            self.stderr.write(self.style.ERROR('No matching chassis found.'))
            return

        self.stdout.write(
            self.style.WARNING(
                'Enabling lldp-peer-info restarts IxServer on each chassis.'
            )
        )
        self.stdout.write(self.style.SUCCESS(f'Targets: {len(chassis)}'))
        self.stdout.write('')

        for ch in chassis:
            label = f'{ch.ip_address} ({ch.chassis_type or "?"}) {ch.hostname or ""}'.strip()
            self.stdout.write(self.style.HTTP_INFO(f'  {label}'))
            try:
                drv = get_driver(ch)
                if not hasattr(drv, 'enable_lldp_peer_info'):
                    self.stdout.write(self.style.ERROR('    SKIP: driver has no enable_lldp_peer_info'))
                    continue
                st = drv.get_lldp_peer_info_status() if hasattr(drv, 'get_lldp_peer_info_status') else None
                if st and st.success and isinstance(st.data, dict):
                    enabled = bool(st.data.get('enabled'))
                    self.stdout.write(f'    status: {st.data.get("raw") or ("enabled" if enabled else "disabled")}')
                    if enabled and skip_enabled:
                        self.stdout.write(self.style.SUCCESS('    already enabled — skip'))
                        continue
                if dry_run:
                    self.stdout.write('    [DRY RUN] would run set lldp-peer-info enabled')
                    continue
                res = drv.enable_lldp_peer_info(confirm_restart=True)
                if res.success:
                    already = (res.data or {}).get('already_enabled')
                    self.stdout.write(
                        self.style.SUCCESS('    OK (already enabled)' if already else '    OK (enabled; IxServer restarted)')
                    )
                else:
                    self.stdout.write(self.style.ERROR(f'    FAILED: {res.error}'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'    ERROR: {e}'))
