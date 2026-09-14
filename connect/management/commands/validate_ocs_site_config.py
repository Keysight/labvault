"""
Enrich site JSON with live LLDP / SNMP LLDP and optionally fix hostnames.

Example:
  python manage.py validate_ocs_site_config --config site.json --output site.validated.json
  python manage.py validate_ocs_site_config --config site.json --output site.json --fix-hostnames --in-place
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from connect.ocs_site_validate import apply_hostname_fixes, enrich_ocs_site_json


class Command(BaseCommand):
    help = "Add connectivity (LLDP) blocks to site JSON; optional hostname sync from LabVault DB."

    def add_arguments(self, parser):
        parser.add_argument(
            "--config",
            type=str,
            required=True,
            help="Input site JSON path",
        )
        parser.add_argument(
            "--output",
            type=str,
            default="",
            help="Output path (default: <input>.validated.json)",
        )
        parser.add_argument(
            "--fix-hostnames",
            action="store_true",
            help="Set each block's name to resolved hostname_db from connectivity (after enrich)",
        )
        parser.add_argument(
            "--in-place",
            action="store_true",
            help="Overwrite the input file instead of --output",
        )
        parser.add_argument(
            "--db-only",
            action="store_true",
            help="Fast path: only DB hostname/status, no live LLDP/SNMP to devices",
        )

    def handle(self, *args, **options):
        src = Path(options["config"])
        if not src.is_file():
            raise CommandError(f"Not found: {src}")
        out = (options.get("output") or "").strip()
        if not out:
            out = str(src.with_suffix("")) + ".validated.json"
        outp = Path(out)
        if options["in_place"]:
            outp = src

        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)
        enriched = enrich_ocs_site_json(data, live_lldp=not options["db_only"])
        if options["fix_hostnames"]:
            enriched, logs = apply_hostname_fixes(enriched)
            for ln in logs:
                self.stdout.write(self.style.SUCCESS(f"fix: {ln}"))
        outp.parent.mkdir(parents=True, exist_ok=True)
        with open(outp, "w", encoding="utf-8") as f:
            json.dump(enriched, f, indent=2, ensure_ascii=False)
        self.stdout.write(self.style.SUCCESS(f"Wrote {outp}"))
