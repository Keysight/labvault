"""
Enable dual-stack (IPv4 then IPv6 failover) for OCS lab inventory.

Does not clear or overwrite mgmt_ipv6 — only sets preferred_ip_version=dual on
devices/chassis tagged ocs-lab (or site 10-36-84). Safe to re-run.

  python manage.py enable_ocs_lab_dual_stack
  python manage.py enable_ocs_lab_dual_stack --dry-run
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from connect.models import Device, KeysightChassis


class Command(BaseCommand):
    help = "Set preferred_ip_version=dual on OCS lab devices and Keysight chassis."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print rows that would be updated without saving",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        dev_q = Device.objects.filter(
            Q(tags__icontains="ocs-lab") | Q(site__icontains="10-36-84")
        ).exclude(preferred_ip_version="dual")
        ch_q = KeysightChassis.objects.filter(
            Q(team_tags__icontains="ocs-lab") | Q(site__icontains="10-36-84")
        ).exclude(preferred_ip_version="dual")

        dev_count = dev_q.count()
        ch_count = ch_q.count()
        if dry:
            for d in dev_q[:50]:
                self.stdout.write(f"  [device] {d.ip_address}  v6={d.mgmt_ipv6 or '—'}")
            for c in ch_q[:50]:
                self.stdout.write(f"  [chassis] {c.ip_address}  v6={c.mgmt_ipv6 or '—'}")
            self.stdout.write(
                self.style.NOTICE(
                    f"[dry-run] Would set dual on {dev_count} device(s), {ch_count} chassis"
                )
            )
            return

        updated_d = dev_q.update(preferred_ip_version="dual")
        updated_c = ch_q.update(preferred_ip_version="dual")
        self.stdout.write(
            self.style.SUCCESS(
                f"Set dual-stack mode on {updated_d} device(s), {updated_c} chassis"
            )
        )
