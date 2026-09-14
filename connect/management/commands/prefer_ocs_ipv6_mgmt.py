"""
Switch OCS lab inventory to prefer IPv6 for management (after validation).

  python manage.py validate_ocs_ipv6_mgmt   # must pass first
  python manage.py prefer_ocs_ipv6_mgmt
  python manage.py prefer_ocs_ipv6_mgmt --dry-run
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from connect.models import Device, KeysightChassis


class Command(BaseCommand):
    help = 'Set preferred_ip_version=ipv6 on OCS lab rows after validate_ocs_ipv6_mgmt passes.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--force', action='store_true', help='Skip validation (not recommended)')
        parser.add_argument('--tag', default='ocs-lab')

    def handle(self, *args, **options):
        if not options['force']:
            try:
                call_command('validate_ocs_ipv6_mgmt', tag=options['tag'])
            except SystemExit:
                raise CommandError(
                    'IPv6 validation failed — fix mgmt_ipv6 / reachability first, '
                    'or use --force to override.',
                ) from None

        tag = options['tag']
        dev_q = Device.objects.filter(
            Q(tags__icontains=tag) | Q(site__icontains='10-36-84'),
        ).exclude(preferred_ip_version='ipv6')
        ch_q = KeysightChassis.objects.filter(
            Q(team_tags__icontains=tag) | Q(site__icontains='10-36-84'),
        ).exclude(preferred_ip_version='ipv6')

        if options['dry_run']:
            self.stdout.write(self.style.NOTICE(
                f'[dry-run] Would set ipv6 on {dev_q.count()} devices, {ch_q.count()} chassis',
            ))
            return

        d = dev_q.update(preferred_ip_version='ipv6')
        c = ch_q.update(preferred_ip_version='ipv6')
        self.stdout.write(self.style.SUCCESS(
            f'Set prefer IPv6 on {d} device(s), {c} chassis',
        ))
