"""
F5 BIG-IP — iControl REST (HTTPS) for probe, system info, and interfaces.
SNMP alone is often disabled; use the device username/password (same as web UI / API).
"""
import logging
import re

import requests
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning

from .base import BaseDriver, DriverResult
from .keysight import KeysightDriver

logger = logging.getLogger(__name__)
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


class F5Driver(BaseDriver):
    VENDOR_NAME = 'f5'
    IC_PREFIX = '/mgmt/tm'

    def _port(self):
        return self.api_port or 443

    def _url(self, path):
        return f'https://{self.ip}:{self._port()}{path}'

    def _get(self, path):
        try:
            r = requests.get(
                self._url(path),
                auth=HTTPBasicAuth(self.username, self.password),
                verify=False,
                timeout=20,
            )
            return r
        except requests.RequestException as e:
            logger.warning('F5 REST %s: %s', path, e)
            return None

    def probe(self) -> str:
        r = self._get(f'{self.IC_PREFIX}/sys/version')
        if r is None:
            return 'unreachable'
        if r.status_code == 200:
            return 'ok'
        if r.status_code in (401, 403):
            return 'auth_failed'
        return 'unreachable'

    def get_base_url(self, proto=None) -> str:
        return f'https://{self.ip}:{self._port()}'

    def get_system_info(self) -> DriverResult:
        r = self._get(f'{self.IC_PREFIX}/sys/version')
        if not r or r.status_code != 200:
            return DriverResult(success=False, error='F5 iControl: could not read /sys/version (HTTPS port, credentials, or API access?)')
        try:
            ver_data = r.json()
        except Exception as e:
            return DriverResult(success=False, error=str(e))
        version_str = ''
        model = 'BIG-IP'
        items = ver_data.get('items') or []
        if items:
            raw = items[0].get('apiRawValues') or items[0]
            if isinstance(raw, dict):
                version_str = (
                    raw.get('Version')
                    or raw.get('fullVersion')
                    or raw.get('version')
                    or ''
                )
                model = raw.get('Product') or raw.get('product') or model
        hostname = self.ip
        rg = self._get(f'{self.IC_PREFIX}/sys/global-settings')
        if rg and rg.status_code == 200:
            try:
                gj = rg.json()
                if isinstance(gj.get('hostname'), str):
                    hostname = gj['hostname']
                elif gj.get('items'):
                    h = gj['items'][0].get('hostname')
                    if h:
                        hostname = h
            except Exception:
                pass
        serial = ''
        rh = self._get(f'{self.IC_PREFIX}/sys/hardware')
        if rh and rh.status_code == 200:
            try:
                hj = rh.json()
                for item in hj.get('items', []):
                    sn = item.get('serialNumber') or item.get('serial_number')
                    if sn:
                        serial = str(sn)
                        break
                    # chassis naming varies
                    nm = (item.get('name') or '') + (item.get('fullPath') or '')
                    if 'chassis' in nm.lower() and item.get('boardSerial'):
                        serial = str(item['boardSerial'])
                        break
            except Exception:
                pass

        return DriverResult(success=True, data={
            'hostname': hostname,
            'version': str(version_str)[:200],
            'model_name': str(model)[:200],
            'serial_number': serial[:100],
            'mac_address': '',
            'uptime': None,
        })

    def get_interfaces(self) -> DriverResult:
        r = self._get(f'{self.IC_PREFIX}/net/interfaces')
        if not r or r.status_code != 200:
            return DriverResult(success=False, error='F5 iControl: could not read /net/interfaces')
        try:
            data = r.json()
        except Exception as e:
            return DriverResult(success=False, error=str(e))
        physical_data = []
        for item in data.get('items', []):
            name = (item.get('name') or item.get('fullPath') or '').strip()
            if not name:
                continue
            short = re.sub(r'^/[^/]+/', '/', name).lstrip('/') or name
            enabled = item.get('enabled', True)
            media_up = bool(item.get('mediaActive'))
            st = str(item.get('status') or '').lower()
            if st in ('up', 'connected'):
                media_up = True
            elif st in ('down', 'uninitialized', 'disabled'):
                media_up = False
            link_speed = item.get('linkSpeed') or item.get('linkSpeedRaw') or 0
            try:
                ls = int(str(link_speed).replace(',', ''))
            except (TypeError, ValueError):
                ls = 0
            status = 'up' if media_up else 'down'
            status_color = 'green' if media_up else 'red'
            admin_status = 'up' if enabled else 'down'
            speed_label = self._speed_label(ls) if ls else ''
            physical_data.append({
                'name': name,
                'short_name': self._short_name(short),
                'status': status,
                'admin_status': admin_status,
                'status_color': status_color,
                'speed_label': speed_label,
            })
        return DriverResult(success=True, data={
            'physical_data': physical_data,
            'logical_data': [],
            'vlan_data': [],
            'port_channel_data': [],
            'management_data': [],
        })

    def get_health(self) -> DriverResult:
        return DriverResult(success=True, data={
            'cpu_utilization': 0,
            'memory_used': 0,
            'memory_total': 0,
        })

    def get_routes(self) -> DriverResult:
        return DriverResult(success=True, data=[])

    def get_vlans(self) -> DriverResult:
        return DriverResult(success=True, data=[])

    def get_running_config(self) -> DriverResult:
        return DriverResult(success=False, error='Not exposed via read-only iControl here')

    def get_startup_config(self) -> DriverResult:
        return DriverResult(success=False, error='Not supported')

    def execute_command(self, command: str) -> DriverResult:
        return DriverResult(success=False, error='F5: use TMSH on the appliance or extend iControl scripts')

    def get_lldp_neighbors_detail(self) -> DriverResult:
        """Delegate to SNMP LLDP if SNMP is reachable; otherwise empty."""
        k = KeysightDriver(self.device)
        return k.get_lldp_neighbors_detail()
