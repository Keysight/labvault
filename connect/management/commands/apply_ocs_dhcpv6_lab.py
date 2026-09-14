"""
Apply OCS DHCPv6 labeling for a customer-provided IPv4 prefix.

Marks inventory as DHCPv6 and prefer-IPv6 for **display**. Does not store a
derived global in ``mgmt_ipv6`` — connections use DHCP IPv4 (and FQDN hostname
when DNS works) until a real IPv6 is observed on the wire.

  python manage.py apply_ocs_dhcpv6_lab --ipv4-prefix 192.0.2
  python manage.py apply_ocs_dhcpv6_lab --ipv4-prefix 192.0.2 --dry-run
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from connect.ip_addressing import derive_dhcpv6_ocs_lab
from connect.models import Device, KeysightChassis


class Command(BaseCommand):
    help = 'Mark tagged OCS rows as DHCPv6 + prefer IPv6 (display); connect stays on DHCP v4/FQDN.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--probe', action='store_true', help='Probe IPv6 after apply')
        parser.add_argument('--tag', default='ocs-lab')
        parser.add_argument(
            '--ipv4-prefix',
            required=True,
            help='First three IPv4 octets of the OCS management subnet, e.g. 192.0.2',
        )

    def handle(self, *args, **options):
        tag = options['tag']
        dry = options['dry_run']
        prefix = (options['ipv4_prefix'] or '').strip().rstrip('.')
        parts = prefix.split('.')
        if len(parts) != 3:
            raise CommandError('--ipv4-prefix must be three octets, e.g. 192.0.2')
        subnet = tuple(parts)
        by_prefix = Q(ip_address__startswith=prefix + '.')
        dev_q = Device.objects.filter(Q(tags__icontains=tag) | by_prefix)
        ch_q = KeysightChassis.objects.filter(Q(team_tags__icontains=tag) | by_prefix)
        updated = 0
        for d in dev_q:
            inferred = derive_dhcpv6_ocs_lab(d.ip_address, ipv4_subnet=subnet)
            if not inferred:
                self.stdout.write(self.style.WARNING(f'  skip device {d.ip_address} (not {prefix}.x)'))
                continue
            if dry:
                self.stdout.write(
                    f'  [device] {d.ip_address} display≈{inferred} dhcpv6/ipv6 connect→v4/FQDN',
                )
            else:
                d.mgmt_ipv6 = ''
                d.mgmt_ipv6_source = 'dhcpv6'
                d.preferred_ip_version = 'ipv6'
                d.save(update_fields=['mgmt_ipv6', 'mgmt_ipv6_source', 'preferred_ip_version', 'updated_at'])
            updated += 1
        for c in ch_q:
            inferred = derive_dhcpv6_ocs_lab(c.ip_address, ipv4_subnet=subnet)
            if not inferred:
                continue
            if dry:
                self.stdout.write(
                    f'  [chassis] {c.ip_address} display≈{inferred} dhcpv6/ipv6 connect→v4/FQDN',
                )
            else:
                c.mgmt_ipv6 = ''
                c.mgmt_ipv6_source = 'dhcpv6'
                c.preferred_ip_version = 'ipv6'
                c.save(update_fields=['mgmt_ipv6', 'mgmt_ipv6_source', 'preferred_ip_version', 'updated_at'])
            updated += 1
        msg = f'{"[dry-run] Would update" if dry else "Updated"} {updated} row(s) (DHCPv6 display, v4 connect)'
        self.stdout.write(self.style.SUCCESS(msg))
        if options['probe'] and not dry:
            from django.core.management import call_command
            call_command('validate_ocs_ipv6_mgmt', tag=tag)
