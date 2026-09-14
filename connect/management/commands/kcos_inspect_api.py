"""
Management command: python manage.py kcos_inspect_api [--ip IP] [--hostname HOST] [--swagger]

Fetches KCOS REST API responses to inspect available fields, especially
ownership/reservedBy on connections (compute node ports).

Usage:
    python manage.py kcos_inspect_api --ip 192.0.2.10
    python manage.py kcos_inspect_api --hostname merpro3n
"""
import json
import socket

from django.core.management.base import BaseCommand

from connect.models import KeysightChassis
from connect.keysight_drivers.kcos import KCOSDriver


def _all_keys(obj, prefix=''):
    """Recursively collect all keys from a nested dict/list."""
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(f'{prefix}.{k}' if prefix else k)
            keys.update(_all_keys(v, f'{prefix}.{k}' if prefix else k))
    elif isinstance(obj, list) and obj:
        keys.update(_all_keys(obj[0], prefix))
    return keys


class Command(BaseCommand):
    help = 'Inspect KCOS API responses for ownership fields'

    def add_arguments(self, parser):
        parser.add_argument('--ip', type=str, help='Chassis IP')
        parser.add_argument('--hostname', type=str, help='Chassis hostname (e.g. merpro3n)')
        parser.add_argument('--swagger', action='store_true', help='Fetch Swagger JSON')

    def handle(self, *args, **options):
        ip = options.get('ip')
        hostname = options.get('hostname')

        if hostname and not ip:
            try:
                ip = socket.gethostbyname(hostname)
                self.stdout.write(f'Resolved {hostname} -> {ip}')
            except socket.gaierror:
                self.stderr.write(self.style.ERROR(f'Could not resolve hostname: {hostname}'))
                return

        if not ip:
            ch = KeysightChassis.objects.filter(hostname__icontains='merpro').first()
            if ch:
                ip = ch.ip_address
                self.stdout.write(f'Using chassis: {ch.hostname} ({ip})')
            else:
                ch = KeysightChassis.objects.filter(status='online').first()
                if ch:
                    ip = ch.ip_address
                    self.stdout.write(f'Using first online chassis: {ip}')
                else:
                    self.stderr.write(self.style.ERROR('No --ip or --hostname and no chassis found'))
                    return

        ch = KeysightChassis.objects.filter(ip_address=ip).first()
        user = (ch.username if ch else None) or 'admin'
        password = (ch.password if ch else None) or 'admin'

        drv = KCOSDriver(ip, user, password)
        if not drv._authenticate():
            self.stderr.write(self.style.ERROR(f'Auth failed for {ip}'))
            return

        self.stdout.write(self.style.SUCCESS(f'\n=== KCOS API introspection for {ip} ===\n'))

        if options.get('swagger'):
            self._fetch_swagger(drv, ip)
            return

        # Connections - raw keys and ownership
        result = drv._get('/introspection/connections')
        if result.success and isinstance(result.data, list):
            conns = result.data
            self.stdout.write(f'Connections: {len(conns)} records')
            if conns:
                c = conns[0]
                keys = sorted(c.keys())
                self.stdout.write(f'  Keys in first connection: {keys}')
                ownership_candidates = [k for k in keys if any(
                    x in k.lower() for x in ('owner', 'reserve', 'assign', 'team', 'tenant')
                )]
                if ownership_candidates:
                    self.stdout.write(self.style.SUCCESS(f'  Possible ownership fields: {ownership_candidates}'))
                    for k in ownership_candidates:
                        self.stdout.write(f'    {k} = {repr(c.get(k))}')
                else:
                    self.stdout.write('  No owner/reservedBy/assign fields found')
                # Show a connection for merpro3n if present
                for c in conns:
                    if 'merpro3n' in str(c.get('name', '')).lower():
                        self.stdout.write(f'\n  merpro3n connection sample: {json.dumps(c, indent=4)[:800]}...')
                        break
        else:
            self.stdout.write(f'Connections: {result.error or "empty"}')

        # Logical ports
        result = drv._get('/introspection/logicalports')
        if result.success:
            lp = result.data
            lp_list = lp.get('logicalports', lp) if isinstance(lp, dict) else (lp if isinstance(lp, list) else [])
            if isinstance(lp_list, list) and lp_list:
                self.stdout.write(f'\nLogical ports: {len(lp_list)} records')
                keys = sorted(_all_keys(lp_list[0]))
                self.stdout.write(f'  Keys: {keys}')
                oc = [k for k in keys if any(x in k.lower() for x in ('owner', 'reserve', 'assign'))]
                if oc:
                    self.stdout.write(self.style.SUCCESS(f'  Possible ownership: {oc}'))
        else:
            self.stdout.write(f'\nLogical ports: {result.error or "N/A"}')

        # Frontpanel
        result = drv._get('/introspection/debug/hardware/frontpanel')
        if result.success:
            fp = result.data
            fp_list = fp.get('frontpanel', fp) if isinstance(fp, dict) else (fp if isinstance(fp, list) else [])
            if isinstance(fp_list, list) and fp_list:
                self.stdout.write(f'\nFrontpanel: {len(fp_list)} records')
                keys = sorted(_all_keys(fp_list[0]))
                self.stdout.write(f'  Keys: {keys}')
                oc = [k for k in keys if any(x in k.lower() for x in ('owner', 'reserve', 'assign'))]
                if oc:
                    self.stdout.write(self.style.SUCCESS(f'  Possible ownership: {oc}'))
        else:
            self.stdout.write(f'\nFrontpanel: {result.error or "N/A"}')

        self.stdout.write('')

    def _fetch_swagger(self, drv, ip):
        """Fetch OpenAPI/Swagger spec and search for ownership-related schemas."""
        for path in ['/restapi/v3/api-docs', '/api/v2/v3/api-docs', '/restapi/swagger.json']:
            url = f'https://{ip}{path}'
            result = drv._get(url)
            if result.success and isinstance(result.data, dict):
                spec = result.data
                self.stdout.write(f'Swagger spec from {path}')
                # Search for owner/reserve/assign in schemas
                schemas = spec.get('components', {}).get('schemas', {}) or {}
                ownership_schemas = {}
                for name, schema in schemas.items():
                    props = schema.get('properties', {}) if isinstance(schema, dict) else {}
                    oc = [k for k in props if any(x in k.lower() for x in ('owner', 'reserve', 'assign', 'team', 'tenant'))]
                    if oc:
                        ownership_schemas[name] = oc
                if ownership_schemas:
                    self.stdout.write(self.style.SUCCESS(f'  Ownership fields in schemas: {ownership_schemas}'))
                else:
                    self.stdout.write('  No ownership fields found in any schema')
                # Dump Connection schema if present
                for key in ['Connection', 'connection', 'IntrospectionConnection']:
                    if key in schemas:
                        self.stdout.write(f'\n  Schema {key} props: {list(schemas[key].get("properties", {}).keys())}')
                        break
                return
        self.stdout.write('Could not fetch Swagger spec from any known path')
