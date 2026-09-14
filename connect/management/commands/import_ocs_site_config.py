"""
Import photonic OCS site JSON: creates/updates Device rows and optional KeysightChassis.

Example:
  python manage.py import_ocs_site_config
  python manage.py import_ocs_site_config --config resources/ocs_photonic_site.json
  python manage.py import_ocs_site_config --dry-run
"""
import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from connect.ocs_site_config_io import merge_tags_ocs, run_ocs_site_import


class Command(BaseCommand):
    help = "Import OCS + Ares + Arista + keysight_chassis from a site JSON into LabVault Device records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--config",
            type=str,
            default="resources/ocs_photonic_site.json",
            help="Path to the site JSON file (customer-provided)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and print planned actions without writing to the database",
        )

    def handle(self, *args, **options):
        path = Path(options["config"])
        if not path.is_absolute():
            path = Path(os.getcwd()) / path
        if not path.is_file():
            raise CommandError(f"Config file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "version" not in data:
            raise CommandError("JSON must include a 'version' field")

        common = data.get("common_tags") or []
        ocs = data.get("ocs_controller")
        ares = data.get("ares_switches") or []
        arista = data.get("arista_switches") or []
        ks = data.get("keysight_chassis") or []

        if not ocs and not ares and not arista and not ks:
            raise CommandError(
                "JSON has no ocs_controller, ares_switches, arista_switches, or keysight_chassis"
            )

        if options["dry_run"]:
            self.stdout.write(self.style.NOTICE(f"[dry-run] Reading: {path}"))
            all_blocks = []
            if ocs:
                all_blocks.append(("ocs", ocs))
            for b in ares:
                all_blocks.append(("ares", b))
            for b in arista:
                all_blocks.append(("arista", b))
            for label, b in all_blocks:
                if not b or not b.get("ip"):
                    continue
                t = merge_tags_ocs(common, b.get("tags") or [], "")
                vendor = b.get("vendor_type") or ("ocs" if label == "ocs" else "arista")
                name = b.get("name") or b.get("ip")
                self.stdout.write(
                    f"  [{label}] {b.get('ip')}  hostname={name}  vendor={vendor}  tags={t}"
                )
            for row in ks:
                if isinstance(row, dict) and row.get("ip"):
                    self.stdout.write(
                        f"  [keysight_chassis] {row.get('ip')}  name={row.get('name', '')}"
                    )
            self.stdout.write(self.style.WARNING("[dry-run] No database changes made."))
            return

        with transaction.atomic():
            result = run_ocs_site_import(data)

        if not result.get("ok"):
            raise CommandError(result.get("error") or "import failed")

        for line in result.get("device_log") or []:
            self.stdout.write(self.style.SUCCESS(f"  {line}"))

        ks_c = result.get("keysight_created", 0)
        ks_u = result.get("keysight_updated", 0)
        if ks_c or ks_u:
            self.stdout.write(
                self.style.SUCCESS(f"  Keysight chassis: created={ks_c} updated={ks_u}")
            )
        for e in result.get("keysight_errors") or []:
            self.stdout.write(self.style.WARNING(f"  WARN: {e}"))

        total = len(result.get("device_log") or []) + ks_c + ks_u
        self.stdout.write(
            self.style.SUCCESS(f"\nDone — {total} device(s) processed from {path.name}")
        )
