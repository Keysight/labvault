"""
Compare live OCS cross-connects to a customer fabric schema and apply gaps.

  python manage.py repair_ocs_patches --ocs-ip 192.0.2.10 --schema /path/to/fabric_schema.json
  python manage.py repair_ocs_patches --ocs-ip 192.0.2.10 --schema /path/to/fabric_schema.json --dry-run
  python manage.py repair_ocs_patches --ocs-ip 192.0.2.10 --schema /path/to/fabric_schema.json --apply
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from connect.drivers import get_driver
from connect.drivers.ocs import _norm_dir
from connect.models import Device


def _live_pairs(device) -> set[tuple[str, str]]:
    driver = get_driver(device)
    rows = driver.fetch_crossconnect_list()
    out: set[tuple[str, str]] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        h1 = row.get('half1') or {}
        conn = str(h1.get('conn') or '')
        m = re.match(r'([^>]+)>([^>]+)', conn)
        if m:
            out.add((m.group(1).strip(), m.group(2).strip()))
    return out


def _schema_pairs(schema_path: Path) -> set[tuple[str, str]]:
    data = json.loads(schema_path.read_text(encoding='utf-8'))
    out: set[tuple[str, str]] = set()
    for p in data.get('active_ocs_paths') or []:
        a = (p.get('ocs_triplet_a') or '').strip()
        b = (p.get('ocs_triplet_b') or '').strip()
        if a and b:
            out.add((a, b))
    return out


def _conn_row(a: str, b: str) -> dict:
    return {
        'in': a,
        'out': b,
        'conn': f'{a}-{b}',
        'group': 'SYSTEM',
        'dir': _norm_dir('bi'),
        'band': 'CBAND',
    }


class Command(BaseCommand):
    help = 'Diff live OCS patches vs a customer fabric schema and optionally apply missing xconnects.'

    def add_arguments(self, parser):
        parser.add_argument('--ocs-ip', required=True, help='OCS controller management IP')
        parser.add_argument(
            '--schema',
            default='',
            help='Fabric schema with active_ocs_paths[] (required with --apply)',
        )
        parser.add_argument(
            '--out',
            default='ocs_pending_patches.json',
            help='Where to write the missing-patch report (cwd by default)',
        )
        parser.add_argument('--dry-run', action='store_true', help='Report only (default)')
        parser.add_argument('--apply', action='store_true', help='POST xconnect_badd for missing pairs')

    def handle(self, *args, **options):
        schema_path = Path(options['schema'])
        if not schema_path.is_file():
            raise CommandError(f'Schema not found: {schema_path}')

        device = Device.objects.filter(
            ip_address=options['ocs_ip'], vendor_type='ocs',
        ).first()
        if not device:
            raise CommandError(f'OCS device not found: {options["ocs_ip"]}')

        expected = _schema_pairs(schema_path)
        live = _live_pairs(device)
        missing = sorted(expected - live)
        extra = sorted(live - expected)

        self.stdout.write(
            f'OCS {device.ip_address}: schema={len(expected)} live={len(live)} '
            f'missing={len(missing)} extra={len(extra)}',
        )
        for a, b in missing:
            self.stdout.write(self.style.WARNING(f'  MISSING  {a} -> {b}'))
        for a, b in extra:
            self.stdout.write(self.style.NOTICE(f'  EXTRA    {a} -> {b}'))

        if not missing:
            self.stdout.write(self.style.SUCCESS('No missing patches.'))
            return

        rows = [_conn_row(a, b) for a, b in missing]
        out_path = Path(options['out'])
        out_path.write_text(
            json.dumps({'ocs_ip': device.ip_address, 'missing': rows}, indent=2),
            encoding='utf-8',
        )
        self.stdout.write(f'Wrote {out_path}')

        if not options['apply']:
            if not options['dry_run']:
                self.stdout.write(self.style.NOTICE('Use --apply to push missing patches (needs write API user).'))
            return

        driver = get_driver(device)
        cmd = json.dumps({'op': 'xconnect_badd', 'connections': rows})
        try:
            res = driver.send_config([cmd])
        except Exception as exc:
            raise CommandError(f'Apply failed: {exc}') from exc
        if not res.success:
            raise CommandError(
                f'Apply failed: {res.error or "unknown"} — OCS user may be read-only (HTTP 403). '
                f'Add patches manually or update device credentials, then re-run --apply.',
            )
        self.stdout.write(self.style.SUCCESS(f'Applied {len(rows)} patch(es).'))
        after = _live_pairs(device)
        still = sorted(expected - after)
        if still:
            self.stdout.write(self.style.WARNING(f'Still missing after apply: {still}'))
