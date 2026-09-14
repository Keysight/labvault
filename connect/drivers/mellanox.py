"""
Mellanox / NVIDIA Spectrum (ONYX) — tries HTTPS JSON UI first, then SNMP (IF-MIB + LLDP).
Ensure username/password match switch login; set SNMP community for SNMP-only paths.
"""
import logging
import re

import requests
from urllib3.exceptions import InsecureRequestWarning

from .base import BaseDriver, DriverResult
from .keysight import KeysightDriver

logger = logging.getLogger(__name__)
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

OID_IF_DESCR = '1.3.6.1.2.1.2.2.1.2'
OID_IF_OPER_STATUS = '1.3.6.1.2.1.2.2.1.8'


class MellanoxDriver(KeysightDriver):
    VENDOR_NAME = 'mellanox'

    def __init__(self, device):
        super().__init__(device)
        self._onyx_session = None

    def _snmp_walk_local(self, oid):
        return self._snmp_walk(oid)

    # ---- ONYX HTTPS JSON (Spectrum switches with ONYX management) ----

    def _onyx_base_url(self):
        port = self.api_port or 443
        return f'https://{self.ip}:{port}'

    def _get_onyx_session(self):
        if self._onyx_session is not None:
            return self._onyx_session
        s = requests.Session()
        s.verify = False
        try:
            r = s.post(
                f'{self._onyx_base_url()}/json/login',
                json={'username': self.username, 'password': self.password},
                timeout=15,
            )
            if r.status_code != 200:
                self._onyx_session = False
                return None
            try:
                j = r.json()
            except Exception:
                self._onyx_session = False
                return None
            ok = (
                j.get('status') == 'OK'
                or j.get('execution_status') == 'OK'
                or j.get('authenticated')
                or j.get('success')
            )
            if ok or r.cookies:
                self._onyx_session = s
                return s
        except requests.RequestException as e:
            logger.debug('ONYX login attempt failed for %s: %s', self.ip, e)
        self._onyx_session = False
        return None

    def _onyx_get_system(self):
        s = self._get_onyx_session()
        if not s:
            return None
        for path in ('/json/system', '/json/get_system_info', '/api/system'):
            try:
                r = s.get(f'{self._onyx_base_url()}{path}', timeout=15)
                if r.status_code == 200:
                    return r.json()
            except requests.RequestException:
                continue
        return None

    def _onyx_get_interfaces_json(self):
        s = self._get_onyx_session()
        if not s:
            return None
        for path in ('/json/interfaces', '/json/ports', '/api/v1/interfaces'):
            try:
                r = s.get(f'{self._onyx_base_url()}{path}', timeout=20)
                if r.status_code == 200:
                    return r.json()
            except requests.RequestException:
                continue
        return None

    def probe(self) -> str:
        if self._get_onyx_session():
            return 'ok'
        return super().probe()

    def get_system_info(self) -> DriverResult:
        js = self._onyx_get_system()
        if isinstance(js, dict):
            hostname = (
                js.get('hostname')
                or js.get('name')
                or js.get('switch_name')
                or self.ip
            )
            ver = js.get('version') or js.get('sw_version') or js.get('fw_version') or ''
            model = js.get('model') or js.get('product_name') or js.get('part_number') or 'Mellanox'
            serial = js.get('serial') or js.get('serial_number') or ''
            return DriverResult(success=True, data={
                'hostname': str(hostname)[:255],
                'version': str(ver)[:200],
                'model_name': str(model)[:200],
                'serial_number': str(serial)[:100],
                'mac_address': '',
                'uptime': None,
            })
        base = super().get_system_info()
        if base.success and base.data:
            base.data.setdefault('model_name', base.data.get('model', ''))
        return base

    def _snmp_if_mib_interfaces(self) -> DriverResult:
        """Standard IF-MIB — works on most switches with SNMP enabled."""
        try:
            descr_by = {}
            for suffix, val in self._snmp_walk_local(OID_IF_DESCR) or []:
                if val:
                    descr_by[suffix] = (val or '').strip()
            oper_by = {}
            for suffix, val in self._snmp_walk_local(OID_IF_OPER_STATUS) or []:
                try:
                    oper_by[suffix] = int(str(val).split()[0])
                except (ValueError, IndexError):
                    oper_by[suffix] = 0
            physical_data = []
            for suffix, name in descr_by.items():
                if not name or name.lower().startswith('null'):
                    continue
                # Skip common virtual / loopback ifDescr noise
                if re.match(r'^(lo|NULL|Nu)$', name, re.I):
                    continue
                op = oper_by.get(suffix, 0)
                up = op == 1
                physical_data.append({
                    'name': name,
                    'short_name': self._short_name(name),
                    'status': 'up' if up else 'down',
                    'admin_status': 'up' if up else 'down',
                    'status_color': 'green' if up else 'red',
                    'speed_label': '',
                })
            return DriverResult(success=True, data={
                'physical_data': physical_data,
                'logical_data': [],
                'vlan_data': [],
                'port_channel_data': [],
                'management_data': [],
            })
        except Exception as e:
            logger.warning('Mellanox IF-MIB walk failed for %s: %s', self.ip, e)
            return DriverResult(success=False, error=str(e))

    def get_interfaces(self) -> DriverResult:
        raw = self._onyx_get_interfaces_json()
        if raw is not None:
            physical_data = []
            items = raw
            if isinstance(raw, dict):
                items = (
                    raw.get('interfaces')
                    or raw.get('items')
                    or raw.get('data')
                    or raw.get('ports')
                    or []
                )
            if isinstance(items, dict):
                items = list(items.values())
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = (
                        item.get('name')
                        or item.get('ifname')
                        or item.get('interface')
                        or item.get('port')
                        or ''
                    )
                    if not name:
                        continue
                    st = str(item.get('oper_status') or item.get('status') or '').lower()
                    up = st in ('up', 'connected', '1', 'true') or item.get('link') == 'up'
                    physical_data.append({
                        'name': name,
                        'short_name': self._short_name(str(name)),
                        'status': 'up' if up else 'down',
                        'admin_status': 'up' if up else 'down',
                        'status_color': 'green' if up else 'red',
                        'speed_label': str(item.get('speed') or item.get('speed_gbps') or ''),
                    })
                if physical_data:
                    return DriverResult(success=True, data={
                        'physical_data': physical_data,
                        'logical_data': [],
                        'vlan_data': [],
                        'port_channel_data': [],
                        'management_data': [],
                    })
        snmp_if = self._snmp_if_mib_interfaces()
        if snmp_if.success and snmp_if.data.get('physical_data'):
            return snmp_if
        return DriverResult(success=True, data={
            'physical_data': [],
            'logical_data': [],
            'vlan_data': [],
            'port_channel_data': [],
            'management_data': [],
            'fetch_degraded': True,
            'fetch_warning': (
                'No ONYX JSON or IF-MIB data. Enable SNMP read-only on the switch, '
                'set the SNMP community on this device, and confirm HTTPS login works.'
            ),
        })
