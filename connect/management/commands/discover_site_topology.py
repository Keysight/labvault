"""
Run LLDP topology discovery scoped to a site tag (avoids fleet-wide timeouts).

Example:
  python manage.py discover_site_topology --tag photonic-10-36-84 --no-chassis
"""
from django.core.management.base import BaseCommand

from connect.topology import discover_topology


class Command(BaseCommand):
    help = (
        "Run discover_topology() for devices matching a tag substring. "
        "Use with import_ocs_site_config + online devices to fill TopologyLink."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tag",
            type=str,
            default="",
            help="Substring of Device.tags to match (e.g. photonic-10-36-84). Empty = full fleet.",
        )
        parser.add_argument(
            "--no-chassis",
            action="store_true",
            help="Do not SNMP-scan Keysight chassis (faster for photonic + switch sites).",
        )
        parser.add_argument(
            "--pool-timeout",
            type=float,
            default=300.0,
            help="Thread-pool timeout for LLDP batch (seconds).",
        )

    def handle(self, *args, **options):
        tag = (options.get("tag") or "").strip()
        r = discover_topology(
            scan_tag=tag,
            include_chassis=not options["no_chassis"],
            pool_timeout=float(options["pool_timeout"] or 300.0),
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"nodes={len(r.get('nodes') or [])} links={len(r.get('links') or [])}"
            )
        )
        for e in (r.get("errors") or [])[:30]:
            self.stdout.write(self.style.WARNING(f"  error: {e}"))
