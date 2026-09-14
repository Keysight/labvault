"""
Validate IPv6 management readiness for OCS lab inventory.

Checks each ocs-lab device/chassis for:
  - mgmt_ipv6 populated in DB
  - IPv6 reachable (ping6 when available, else driver probe on IPv6 target)

Exit code 0 only when all rows pass. Use before prefer_ocs_ipv6_mgmt.

  python manage.py validate_ocs_ipv6_mgmt
  python manage.py validate_ocs_ipv6_mgmt --json
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List

from django.core.management.base import BaseCommand
from django.db.models import Q

from connect.drivers import get_driver
from connect.ip_addressing import normalize_ip, resolve_connect_targets, resolve_mgmt_ipv6
from connect.keysight_drivers import get_driver as get_chassis_driver
from connect.models import Device, KeysightChassis


def _ping6(host: str, timeout: int = 2) -> bool:
    if not host or not normalize_ip(host):
        return False
    try:
        r = subprocess.run(
            ['ping6', '-c', '1', '-W', str(timeout), host],
            capture_output=True,
            timeout=timeout + 2,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _probe_entity(entity, *, kind: str) -> Dict[str, Any]:
    v4 = (getattr(entity, 'ip_address', '') or '').strip()
    v6 = normalize_ip(resolve_mgmt_ipv6(
        ipv4=v4,
        mgmt_ipv6=getattr(entity, 'mgmt_ipv6', '') or '',
        ipv6_source=getattr(entity, 'mgmt_ipv6_source', '') or '',
    ))
    pref = getattr(entity, 'preferred_ip_version', 'auto') or 'auto'
    targets = resolve_connect_targets(
        ipv4=v4, ipv6=v6, preferred='ipv6' if v6 else pref, hostname='',
    )
    row: Dict[str, Any] = {
        'kind': kind,
        'ip_v4': v4,
        'mgmt_ipv6': v6 or None,
        'preferred': pref,
        'targets': targets,
        'ok': False,
        'issues': [],
    }
    if not v6:
        row['issues'].append('mgmt_ipv6 not set in inventory')
        return row
    if _ping6(v6):
        row['ping6'] = True
    else:
        row['ping6'] = False
        row['issues'].append('ping6 failed')
    try:
        if kind == 'device':
            drv = get_driver(entity)
        else:
            drv = get_chassis_driver(entity)
        saved = drv.ip if hasattr(drv, 'ip') else v4
        drv.ip = v6
        st = drv.probe()
        row['probe_ipv6'] = st
        if hasattr(drv, 'ip'):
            drv.ip = saved
        if st == 'ok':
            row['ok'] = True
            row['issues'] = [i for i in row['issues'] if i != 'ping6 failed']
        else:
            row['issues'].append(f'probe on IPv6 returned {st}')
    except Exception as exc:
        row['issues'].append(f'probe error: {exc}')
    if row.get('probe_ipv6') == 'ok':
        row['ok'] = True
        row['issues'] = [i for i in row['issues'] if i != 'ping6 failed']
    elif row.get('ping6'):
        row['ok'] = True
    return row


class Command(BaseCommand):
    help = 'Validate OCS lab IPv6 management addresses before cutover to prefer IPv6.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', help='Machine-readable JSON report')
        parser.add_argument(
            '--tag',
            default='ocs-lab',
            help='Filter devices/chassis by tag (default: ocs-lab)',
        )

    def handle(self, *args, **options):
        tag = (options['tag'] or 'ocs-lab').strip()
        dev_q = Device.objects.filter(
            Q(tags__icontains=tag) | Q(site__icontains='10-36-84'),
        ).order_by('ip_address')
        ch_q = KeysightChassis.objects.filter(
            Q(team_tags__icontains=tag) | Q(site__icontains='10-36-84'),
        ).order_by('ip_address')

        report: List[Dict[str, Any]] = []
        for d in dev_q:
            report.append(_probe_entity(d, kind='device'))
        for c in ch_q:
            report.append(_probe_entity(c, kind='chassis'))

        ok_count = sum(1 for r in report if r['ok'])
        fail = [r for r in report if not r['ok']]

        if options['json']:
            self.stdout.write(json.dumps({
                'ok': len(fail) == 0,
                'passed': ok_count,
                'total': len(report),
                'rows': report,
            }, indent=2))
        else:
            self.stdout.write(self.style.NOTICE(
                f'OCS IPv6 validation: {ok_count}/{len(report)} passed',
            ))
            for r in report:
                label = f"{r['kind']:7} {r['ip_v4']:16} v6={r.get('mgmt_ipv6') or '—'}"
                if r['ok']:
                    self.stdout.write(self.style.SUCCESS(f'  OK  {label}'))
                else:
                    self.stdout.write(self.style.ERROR(
                        f"  FAIL {label}  ({'; '.join(r['issues'])})",
                    ))
            if fail:
                self.stdout.write(self.style.WARNING(
                    '\nNo cutover: set mgmt_ipv6 (or dhcpv6 source + LABVAULT_OCS_IPV4_PREFIX), '
                    'then re-run. ICMPv6 may be filtered; driver probe=ok still counts.',
                ))

        if fail:
            raise SystemExit(1)
