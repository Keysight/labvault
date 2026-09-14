"""Periodic scan: detect moves, offline thresholds, and BMC IPMI reachability."""
from django.core.management.base import BaseCommand

from connect.changelog import scan_all_bmcs, scan_all_targets


class Command(BaseCommand):
    help = (
        'Scan devices, Keysight chassis, and BMC endpoints (IPMI) for moves, '
        'offline thresholds, and BMC up/down events in the change log.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--bmc-only',
            action='store_true',
            help='Only probe BMC endpoints via IPMI (faster).',
        )
        parser.add_argument(
            '--bmc-timeout',
            type=int,
            default=6,
            help='IPMI timeout per BMC in seconds (default 6).',
        )

    def handle(self, *args, **options):
        if options['bmc_only']:
            stats = scan_all_bmcs(timeout=options['bmc_timeout'])
            self.stdout.write(
                self.style.SUCCESS(
                    f"changelog_scan (BMC only): {stats['bmcs']} endpoints, "
                    f"{stats.get('reachable', 0)} reachable, "
                    f"{stats.get('unreachable', 0)} unreachable, "
                    f"{stats['events']} events emitted"
                )
            )
            return

        stats = scan_all_targets(bmc_timeout=options['bmc_timeout'])
        self.stdout.write(
            self.style.SUCCESS(
                f"changelog_scan: {stats['devices']} devices, {stats['chassis']} chassis, "
                f"{stats['bmcs']} BMCs ({stats.get('bmc_reachable', 0)} up / "
                f"{stats.get('bmc_unreachable', 0)} down), "
                f"{stats['events']} events emitted this run"
            )
        )
