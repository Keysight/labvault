"""
IxOS REST API Driver for Keysight / Ixia chassis.

Authentication : POST /platform/api/v1/auth/session  -> apiKey
Base URL       : https://{ip}/chassis/api/v2/ixos
Swagger docs   : https://{ip}/chassis/swagger/index.html
                 https://{ip}/platform/swagger/index.html

Supports both Linux-based (XGS12, APS) and Windows-based chassis with
graceful degradation for missing endpoints.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

logger = logging.getLogger(__name__)

from ..ip_addressing import bracket_host, unbracket_host


# ---------------------------------------------------------------------------
# Result wrapper
# ---------------------------------------------------------------------------

@dataclass
class DriverResult:
    success: bool = False
    data: Any = None
    error: str = ''


# ---------------------------------------------------------------------------
# Session cache  (api_key per IP so we don't re-auth every call)
# ---------------------------------------------------------------------------
_api_key_cache: dict[str, str] = {}
_session_pool: dict[str, requests.Session] = {}


def _get_session(ip: str) -> requests.Session:
    if ip not in _session_pool:
        s = requests.Session()
        s.verify = False
        _session_pool[ip] = s
    return _session_pool[ip]


_PCPU_MGMT_RE = re.compile(r'^10\.0\.\d+\.\d+$')


def fill_inferred_pcpu_mgmt_ips(ports: list[dict]) -> list[dict]:
    """Fill missing per-port PCPU management IPs from a validated card pattern.

    Ixia/AresONE/XGS12 PCPUs sit on an internal ``10.0.<card>.<port>`` network.
    Many ports report ``managementIp`` directly; some (down/unowned) do not. For
    each card, if *every* reported management IP matches ``10.0.<card>.<port>``,
    we treat the pattern as confirmed and infer the missing siblings. The strict
    validation means chassis that use a different scheme (e.g. some AresONE-M
    layouts) are never given fabricated IPs.
    """
    from collections import defaultdict

    by_card: Dict[Any, list] = defaultdict(list)
    for p in ports:
        if isinstance(p, dict):
            by_card[p.get('card_number')].append(p)

    for card, plist in by_card.items():
        if card is None:
            continue
        try:
            card_i = int(card)
        except (TypeError, ValueError):
            continue
        confirmed = []
        for p in plist:
            ip = (p.get('management_ip') or '').strip()
            pn = p.get('port_number')
            if ip and pn is not None:
                confirmed.append((pn, ip))
        if not confirmed:
            continue
        if not all(ip == f'10.0.{card_i}.{pn}' for pn, ip in confirmed):
            continue  # pattern not confirmed for this card — do not fabricate
        for p in plist:
            pn = p.get('port_number')
            if pn is None:
                continue
            if not (p.get('management_ip') or '').strip():
                p['management_ip'] = f'10.0.{card_i}.{pn}'
                p['management_ip_inferred'] = True
    return ports


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class IxOSDriver:
    """Driver for a single IxOS chassis."""

    def __init__(
        self,
        ip: str,
        username: str = 'admin',
        password: str = 'admin',
        *,
        hostname: str = '',
        chassis_type: str = '',
    ):
        self._host_raw = (ip or '').strip()
        self.ip = bracket_host(self._host_raw)
        self.username = username
        self.password = password
        self.hostname = (hostname or '').strip()
        self.chassis_type = (chassis_type or '').lower().replace('-', '').replace('_', '')
        self.api_key: str | None = _api_key_cache.get(ip)
        self._auth_uri = '/platform/api/v1/auth/session'
        self._last_ssh_error: str = ''

    def _ssh_hosts(self) -> list[str]:
        """Management IPs/hosts to try for SSH (hostname often works when IP does not)."""
        out: list[str] = []
        for h in (self.hostname, unbracket_host(self._host_raw)):
            h = (h or '').strip()
            if h and h not in out:
                out.append(h)
        return out

    def _needs_chassis_cli_prefix(self) -> bool:
        return self.chassis_type in ('xgs12', 'xgs2', 'xm', 'xg', 'ixvm')

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _base_url(self):
        return f'https://{self.ip}/chassis/api/v2/ixos'

    def _headers(self):
        return {
            'Content-Type': 'application/json',
            'x-api-key': self.api_key or '',
        }

    def _authenticate(self) -> bool:
        """Obtain an API key via username/password."""
        session = _get_session(self.ip)
        url = f'https://{self.ip}{self._auth_uri}'
        payload = {
            'username': self.username,
            'password': self.password,
            'rememberMe': False,
            'resetWeakPassword': False,
        }
        try:
            resp = session.post(url, json=payload, headers={'Content-Type': 'application/json'},
                                verify=False, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                self.api_key = data.get('apiKey', '')
                if self.api_key:
                    _api_key_cache[self.ip] = self.api_key
                    return True
            logger.debug(f'IxOS auth failed for {self.ip}: HTTP {resp.status_code}')
            return False
        except Exception as e:
            logger.debug(f'IxOS auth error for {self.ip}: {e}')
            return False

    def _ensure_auth(self) -> bool:
        if self.api_key:
            return True
        return self._authenticate()

    def _request(self, method: str, path: str, payload=None, timeout=12):
        """Issue an HTTP request against the IxOS REST API.
        Returns (response, data) or (None, None) on failure.
        Handles 401 by re-authenticating once."""
        if not self._ensure_auth():
            return None, None

        session = _get_session(self.ip)
        url = path if path.startswith('http') else self._base_url() + path

        for attempt in range(2):
            try:
                resp = session.request(
                    method, url,
                    json=payload,
                    headers=self._headers(),
                    verify=False,
                    timeout=timeout,
                )

                # Re-auth on 401
                if resp.status_code == 401 and attempt == 0:
                    self.api_key = None
                    _api_key_cache.pop(self.ip, None)
                    if self._authenticate():
                        continue
                    return None, None

                data = None
                try:
                    data = resp.json() if resp.text else None
                except Exception:
                    data = resp.text

                return resp, data
            except Exception as e:
                logger.debug(f'IxOS request error {method} {url}: {e}')
                return None, None
        return None, None

    def _get(self, path: str, params=None, timeout=12):
        """Convenience GET that returns DriverResult."""
        if not self._ensure_auth():
            return DriverResult(error='Authentication failed')
        session = _get_session(self.ip)
        url = path if path.startswith('http') else self._base_url() + path
        try:
            resp = session.get(url, params=params, headers=self._headers(),
                               verify=False, timeout=timeout)
            if resp.status_code == 401:
                self.api_key = None
                _api_key_cache.pop(self.ip, None)
                if self._authenticate():
                    resp = session.get(url, params=params, headers=self._headers(),
                                       verify=False, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json() if resp.text else None
                return DriverResult(success=True, data=data)
            return DriverResult(error=f'HTTP {resp.status_code}')
        except Exception as e:
            return DriverResult(error=str(e))

    def _post_operation(self, path: str, timeout=60):
        """POST an operation endpoint and handle async (202) polling."""
        resp, data = self._request('POST', path, timeout=timeout)
        if resp is None:
            return DriverResult(error='Request failed')
        if resp.status_code == 200:
            return DriverResult(success=True, data=data)
        if resp.status_code == 202:
            # Async - poll for completion
            return self._poll_async(data, timeout)
        return DriverResult(error=f'HTTP {resp.status_code}: {data}')

    def _poll_async(self, response_body, timeout=120):
        """Poll an async operation until SUCCESS/COMPLETED/ERROR."""
        if not response_body or not isinstance(response_body, dict):
            return DriverResult(error='Invalid async response')
        poll_url = response_body.get('url', '')
        if not poll_url:
            return DriverResult(error='No poll URL in async response')
        start = time.time()
        while time.time() - start < timeout:
            time.sleep(2)
            resp, data = self._request('GET', poll_url)
            if resp is None:
                continue
            state = data.get('state', '') if isinstance(data, dict) else ''
            if state in ('SUCCESS', 'COMPLETED'):
                result_url = data.get('resultUrl', '')
                if result_url:
                    r2, d2 = self._request('GET', result_url)
                    return DriverResult(success=True, data=d2)
                return DriverResult(success=True, data=data)
            if state == 'ERROR':
                return DriverResult(error=data.get('message', 'Async operation failed'))
            # Still IN_PROGRESS - keep polling
        return DriverResult(error='Async operation timed out')

    # ------------------------------------------------------------------
    # Public API - connectivity
    # ------------------------------------------------------------------

    def probe(self) -> str:
        """Test connectivity. Returns 'ok', 'auth_failed', or 'unreachable'."""
        try:
            if self._authenticate():
                # Quick check that we can actually reach the chassis endpoint
                result = self._get('/chassis', timeout=8)
                if result.success:
                    return 'ok'
                return 'ok'  # auth worked even if chassis endpoint quirky
            return 'auth_failed'
        except Exception:
            return 'unreachable'

    # ------------------------------------------------------------------
    # Public API - chassis info
    # ------------------------------------------------------------------

    def get_chassis_info(self) -> DriverResult:
        """Fetch chassis details: type, serial, state, applications.
        AresONE may expose type via Platform API; IxOS chassis API
        can return empty/different structure, so we try both."""
        result = self._get('/chassis')
        raw = None
        chassis_type = ''
        if result.success and result.data:
            raw = result.data
            if isinstance(raw, list) and raw:
                raw = raw[0]
            if isinstance(raw, dict):
                chassis_type = (raw.get('type') or '').strip()

        # AresONE: IxOS chassis API may return empty type; try Platform API
        if not chassis_type or 'aresone' not in chassis_type.lower():
            platform_type = self._fetch_platform_chassis_type()
            if platform_type:
                chassis_type = platform_type
                logger.debug('IxOS %s: using Platform API chassis type %r', self.ip, chassis_type)

        if not raw and not chassis_type:
            return result if not result.success else DriverResult(error='No chassis data')

        if not isinstance(raw, dict):
            raw = {}

        apps = {}
        for app in raw.get('ixosApplications', []):
            apps[app.get('name', '')] = app.get('version', '')

        info = {
            'management_ip': raw.get('managementIp', self.ip),
            'chassis_type': (chassis_type or raw.get('type', '')).replace(' ', '_'),
            'serial_number': raw.get('serialNumber', ''),
            'controller_serial': raw.get('controllerSerialNumber', ''),
            'state': raw.get('state', 'unknown'),
            'num_physical_cards': raw.get('numberOfPhysicalCards', 0),
            'ixos_applications': apps,
        }
        # Extract IxOS version from applications
        for name, ver in apps.items():
            if 'IxOS' in name:
                info['ixos_version'] = ver
                break
        return DriverResult(success=True, data=info)

    def _fetch_platform_chassis_type(self) -> str:
        """Fetch chassis type from Platform API /platform/api/v1/chassis.
        Used for AresONE where IxOS chassis API may not expose type."""
        if not self._ensure_auth():
            return ''
        url = f'https://{self.ip}/platform/api/v1/chassis'
        session = _get_session(self.ip)
        try:
            resp = session.get(
                url,
                headers=self._headers(),
                verify=False,
                timeout=12,
            )
            if resp.status_code != 200:
                return ''
            data = resp.json()
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                return (data.get('type') or '').strip()
        except Exception as e:
            logger.debug('IxOS Platform API chassis type fetch %s: %s', self.ip, e)
        return ''

    # ------------------------------------------------------------------
    # Public API - cards (slots)
    # ------------------------------------------------------------------

    def get_cards(self) -> DriverResult:
        """Fetch all cards (line cards) in the chassis.
        Returns list of dicts with cardNumber, type, state, numberOfPorts, serialNumber."""
        result = self._get('/cards')
        if not result.success:
            return result
        raw = result.data if isinstance(result.data, list) else []
        cards = []
        for c in sorted(raw, key=lambda x: x.get('cardNumber', 999)):
            cards.append({
                'id': c.get('id', 0),
                'card_number': c.get('cardNumber', 0),
                'type': c.get('type', 'Unknown'),
                'state': c.get('state', 'unknown'),
                'serial_number': c.get('serialNumber', ''),
                'num_ports': c.get('numberOfPorts', 0),
            })
        return DriverResult(success=True, data=cards)

    # ------------------------------------------------------------------
    # Public API - ports
    # ------------------------------------------------------------------

    @staticmethod
    def _port_display_name(raw: dict) -> str:
        """Topology-facing port label (e.g. AresONE ``2.1``), not raw API ``card.port``."""
        fqn = (raw.get('fullyQualifiedPortName') or '').strip()
        if fqn and fqn.upper() != 'N/A':
            return fqn
        pn = raw.get('portNumber')
        try:
            pn_int = int(pn) if pn is not None else None
        except (TypeError, ValueError):
            pn_int = None
        if pn_int is not None and 9 <= pn_int <= 24:
            rg = (pn_int - 9) // 2 + 1
            sub = (pn_int - 9) % 2 + 1
            return f'{rg}.{sub}'
        cn = raw.get('cardNumber', 0)
        return f'{cn}.{pn_int}' if pn_int is not None else str(cn)

    def get_ports(self) -> DriverResult:
        """Fetch all ports across all cards.
        Returns list of dicts with card/port numbers, owner, transceiver,
        link state, speed, phyMode, transmitState."""
        result = self._get('/ports')
        if not result.success:
            return result
        raw = result.data if isinstance(result.data, list) else []
        ports = []
        for p in raw:
            owner = p.get('owner', '') or 'Free'
            link_state_raw = p.get('linkState', 'unknown')
            link_state, led_color = self._normalize_link(link_state_raw, owner)
            port_memory_raw = p.get('portMemory')
            port_memory_kb = None
            if port_memory_raw is not None:
                try:
                    port_memory_kb = float(port_memory_raw)
                except (TypeError, ValueError):
                    port_memory_kb = None
            port_row = {
                'id': p.get('id', 0),
                'card_number': p.get('cardNumber', 0),
                'port_number': p.get('portNumber', 0),
                'owner': owner,
                'link_state': link_state,
                'link_state_raw': link_state_raw,
                'led_color': led_color,
                'speed': p.get('speed', ''),
                'phy_mode': p.get('phyMode', ''),
                'transmit_state': p.get('transmitState', ''),
                'transceiver_model': p.get('transceiverModel', ''),
                'transceiver_mfg': p.get('transceiverManufacturer', ''),
                'type': p.get('type', ''),
                'pcpu_status': p.get('pcpuStatus', ''),
                'port_memory_kb': port_memory_kb,
                'fully_qualified_port_name': (p.get('fullyQualifiedPortName') or '').strip(),
                'management_ip': (p.get('managementIp') or '').strip(),
                'port_display': self._port_display_name(p),
            }
            rgn = p.get('resourceGroupNumber', p.get('resourceGroupId'))
            rg_obj = p.get('resourceGroup')
            if rgn is None and isinstance(rg_obj, dict):
                rgn = rg_obj.get('number', rg_obj.get('id', rg_obj.get('resourceGroupNumber')))
            elif rgn is None and rg_obj is not None:
                rgn = rg_obj
            if rgn is not None:
                try:
                    port_row['resource_group_number'] = int(rgn)
                except (TypeError, ValueError):
                    pass
            ports.append(port_row)
        # Fill missing PCPU mgmt IPs from the confirmed per-card pattern so every
        # packet CPU on a multi-PCPU card is discovered (not just owned ports).
        fill_inferred_pcpu_mgmt_ips(ports)
        # Sort by card then port
        ports.sort(key=lambda x: (x['card_number'], x['port_number']))
        return DriverResult(success=True, data=ports)

    # ------------------------------------------------------------------
    # Public API - health / performance
    # ------------------------------------------------------------------

    def get_pcpu_health_by_mgmt_ip(self) -> DriverResult:
        """Per-PCPU CPU/mem/throughput keyed by internal ``managementIp`` (10.0.x.x).

        Ports on the same packet CPU share one Linux (busybox/dropbear) host on the
        chassis-internal ``10.0.x.x`` network. This applies to AresONE *and* XGS12
        and other Ixia chassis with line-card NPs — we gate on whether any port
        actually exposes a ``10.0.x.x`` management IP rather than on chassis type.
        Requires root SSH on the chassis management host (``ARESONE_ROOT_SSH_*``).
        """
        from pathlib import Path

        from django.conf import settings

        from ..aresone_ssh import collect_pcpu_health

        password = getattr(settings, 'ARESONE_ROOT_SSH_PASSWORD', '') or ''
        key_path = getattr(settings, 'ARESONE_ROOT_SSH_KEY', '') or ''
        user = getattr(settings, 'ARESONE_ROOT_SSH_USER', 'root') or 'root'
        if not password and not (key_path and Path(key_path).is_file()):
            return DriverResult(success=True, data={})

        ports_res = self.get_ports()
        if not ports_res.success or not isinstance(ports_res.data, list):
            return DriverResult(success=True, data={})

        mgmt_ips = sorted({
            (p.get('management_ip') or '').strip()
            for p in ports_res.data
            if isinstance(p, dict) and _PCPU_MGMT_RE.match((p.get('management_ip') or '').strip())
        })
        if not mgmt_ips:
            # No internal PCPU network exposed (e.g. Windows/legacy chassis).
            return DriverResult(success=True, data={})

        data = collect_pcpu_health(
            self._ssh_hosts(),
            mgmt_ips,
            username=user,
            password=password,
            key_path=key_path,
        )
        return DriverResult(success=True, data=data)

    def get_pcpu_apps_by_mgmt_ip(self) -> DriverResult:
        """Per-PCPU IxOS / IxNetwork application versions keyed by ``managementIp``."""
        from pathlib import Path

        from django.conf import settings

        from ..aresone_ssh import collect_pcpu_app_versions

        password = getattr(settings, 'ARESONE_ROOT_SSH_PASSWORD', '') or ''
        key_path = getattr(settings, 'ARESONE_ROOT_SSH_KEY', '') or ''
        user = getattr(settings, 'ARESONE_ROOT_SSH_USER', 'root') or 'root'
        if not password and not (key_path and Path(key_path).is_file()):
            return DriverResult(success=True, data={})

        ports_res = self.get_ports()
        if not ports_res.success or not isinstance(ports_res.data, list):
            return DriverResult(success=True, data={})

        mgmt_ips = sorted({
            (p.get('management_ip') or '').strip()
            for p in ports_res.data
            if isinstance(p, dict) and _PCPU_MGMT_RE.match((p.get('management_ip') or '').strip())
        })
        if not mgmt_ips:
            return DriverResult(success=True, data={})

        data = collect_pcpu_app_versions(
            self._ssh_hosts(),
            mgmt_ips,
            username=user,
            password=password,
            key_path=key_path,
            ixos_username=self.username,
            ixos_password=self.password,
        )
        return DriverResult(success=True, data=data)

    def get_health(self) -> DriverResult:
        """Fetch CPU and memory usage from perfcounters.

        ``/perfcounters`` returns a ring buffer of recent samples (each with an
        incrementing ``sequence``) for the chassis controller (``parentId`` 1000).
        We take the most recent sample so CPU/mem/disk reflect *now*, not the
        oldest buffered value.
        """
        result = self._get('/perfcounters')
        empty = {'cpu_utilization': 0, 'memory_used': 0, 'memory_total': 0, 'disk_io_bps': 0}
        if not result.success:
            return DriverResult(success=True, data=empty)
        raw = result.data
        if isinstance(raw, list):
            rows = [r for r in raw if isinstance(r, dict)]
            if not rows:
                return DriverResult(success=True, data=empty)
            # Latest by sequence (fallback: last element).
            raw = max(rows, key=lambda r: r.get('sequence', 0))
        if not isinstance(raw, dict):
            return DriverResult(success=True, data=empty)
        return DriverResult(success=True, data={
            'cpu_utilization': raw.get('cpuUsagePercent', 0),
            'memory_used': int(raw.get('memoryInUseBytes', 0)),
            'memory_total': int(raw.get('memoryTotalBytes', 0)),
            'disk_io_bps': float(raw.get('diskIOBytesPerSecond', 0) or 0),
        })

    # ------------------------------------------------------------------
    # Public API - sensors
    # ------------------------------------------------------------------

    def get_sensors(self) -> DriverResult:
        """Fetch sensor data (temperature, fan, voltage)."""
        result = self._get('/sensors')
        if not result.success:
            return DriverResult(success=True, data=[])
        raw = result.data if isinstance(result.data, list) else []
        sensors = []
        for s in raw:
            sensors.append({
                'name': s.get('name', ''),
                'unit': s.get('unit', ''),
                'value': s.get('value', 0),
                'type': s.get('sensorType', s.get('type', '')),
                'card_number': s.get('cardNumber', ''),
            })
        return DriverResult(success=True, data=sensors)

    # ------------------------------------------------------------------
    # Public API - port statistics
    # ------------------------------------------------------------------

    def get_port_stats(self) -> DriverResult:
        """Fetch per-port statistics."""
        return self._get('/portstats')

    # ------------------------------------------------------------------
    # Public API - LLDP peers (for topology cross-matching)
    # ------------------------------------------------------------------

    def get_lldp_peers(self) -> DriverResult:
        """Fetch LLDP neighbor info via IxOS REST API.

        Tries dedicated LLDP endpoints first, then parses embedded peer data
        from /ports.  Returns list of {local_port, remote_device, remote_port,
        chassis_id, mgmt_ip}.
        """
        if not self._ensure_auth():
            return DriverResult(error='Authentication failed')

        for path in ('/lldpneighbors', '/lldppeers', '/lldpNeighbors',
                     '/lldp/neighbors', '/neighbors/lldp'):
            r = self._get(path, timeout=12)
            if r.success and r.data:
                parsed = self._parse_lldp_payload(r.data)
                if parsed:
                    return DriverResult(success=True, data=parsed)

        r = self._get('/ports', timeout=18)
        if r.success and isinstance(r.data, list):
            parsed = self._parse_lldp_from_ports(r.data)
            if parsed:
                return DriverResult(success=True, data=parsed)

        return DriverResult(success=True, data=[])

    def _parse_lldp_payload(self, data):
        """Normalize various JSON shapes returned by /lldp* endpoints."""
        if isinstance(data, list):
            return self._normalize_lldp_rows(data)
        if not isinstance(data, dict):
            return []
        for key in ('lldpNeighbors', 'neighbors', 'lldp', 'items', 'data', 'ports'):
            inner = data.get(key)
            if isinstance(inner, list):
                parsed = self._normalize_lldp_rows(inner)
                if parsed:
                    return parsed
        return []

    def _normalize_lldp_rows(self, rows):
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            lp = r.get('localPort', r.get('local_port', ''))
            if isinstance(lp, (int, float)):
                lp = str(int(lp))
            rem = (r.get('systemName') or r.get('system_name') or
                   r.get('remoteSystemName') or r.get('remote_device') or '')
            rport = (r.get('portId') or r.get('port_id') or
                     r.get('remotePortId') or r.get('remote_port') or '')
            if isinstance(rport, dict):
                rport = rport.get('id', '') or rport.get('name', '')
            cid = (r.get('chassisId') or r.get('chassis_id') or '')
            mgmt = (r.get('managementAddress') or r.get('managementIp') or
                    r.get('mgmt_ip') or '')
            if rem or rport or cid:
                out.append({
                    'local_port': str(lp).strip(),
                    'remote_device': str(rem).strip(),
                    'remote_port': str(rport).strip(),
                    'chassis_id': str(cid).strip(),
                    'mgmt_ip': str(mgmt).strip() if mgmt else '',
                })
        return out

    def _parse_lldp_from_ports(self, raw_ports):
        """Extract LLDP from each port object (IxOS may embed neighbor info)."""
        out = []
        for p in raw_ports:
            if not isinstance(p, dict):
                continue
            peer = None
            for k in ('lldpNeighbor', 'lldpNeighborInfo', 'lldpPeer',
                      'lldp', 'neighbor'):
                v = p.get(k)
                if isinstance(v, dict) and (
                    v.get('systemName') or v.get('system_name') or
                    v.get('remoteSystemName')
                ):
                    peer = v
                    break
            if not peer:
                continue
            cn = int(p.get('cardNumber', p.get('card_number', 0)) or 0)
            pn = p.get('portNumber', p.get('port_number'))
            try:
                pn = int(pn) if pn is not None else None
            except (TypeError, ValueError):
                pn = None

            # AresONE maps API port_number 9-24 → resource-group format 1.1 - 8.2
            if pn is not None and 9 <= pn <= 24:
                rg = (pn - 9) // 2 + 1
                sub = (pn - 9) % 2 + 1
                local_port = f'{rg}.{sub}'
            elif cn is not None and pn is not None:
                local_port = f'{cn}.{pn}'
            else:
                local_port = str(pn or '')

            rem = (peer.get('systemName') or peer.get('system_name') or
                   peer.get('remoteSystemName') or '')
            rport = (peer.get('portId') or peer.get('port_id') or
                     peer.get('portDescription') or peer.get('remotePortId') or '')
            if isinstance(rport, dict):
                rport = rport.get('id', '') or rport.get('name', '')
            mgmt = (peer.get('managementAddress') or peer.get('managementIp') or
                    peer.get('mgmtIp') or '')
            out.append({
                'local_port': local_port,
                'remote_device': str(rem).strip(),
                'remote_port': str(rport).strip(),
                'chassis_id': str(peer.get('chassisId') or peer.get('chassis_id') or '').strip(),
                'mgmt_ip': str(mgmt).strip() if mgmt else '',
            })
        return out

    # ------------------------------------------------------------------
    # SSH CLI — IxOS commands via paramiko
    # ------------------------------------------------------------------

    def _ssh_run(self, cmd: str, timeout: int = 12) -> str:
        """Execute an IxOS CLI command via SSH.

        AresONE auto-enters chassis mode so commands work directly.
        XGS12/XGS2 land in the launcher — use ``chassis <cmd>`` first when applicable.
        Tries ``hostname`` then ``ip`` when both are configured.
        """
        import paramiko

        self._last_ssh_error = ''
        base_cmd = (cmd or '').strip()
        if not base_cmd:
            return ''

        if self._needs_chassis_cli_prefix() and not base_cmd.lower().startswith('chassis '):
            commands = [f'chassis {base_cmd}', base_cmd]
        else:
            commands = [base_cmd]
            if not self._needs_chassis_cli_prefix():
                commands.append(f'chassis {base_cmd}')

        for host in self._ssh_hosts():
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    host, username=self.username, password=self.password,
                    timeout=8, look_for_keys=False, allow_agent=False,
                )
                last_out = ''
                for run_cmd in commands:
                    _, stdout, _ = client.exec_command(run_cmd, timeout=timeout)
                    last_out = stdout.read().decode('utf-8', errors='replace')
                    if last_out and 'Unknown or incomplete command' not in last_out:
                        return last_out
                if last_out:
                    return last_out
            except Exception as e:
                self._last_ssh_error = f'{host}: {e}'
                logger.debug('IxOS SSH %s cmd=%r failed: %s', host, base_cmd, e)
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        return ''

    # --- show topology ---------------------------------------------------

    def get_topology_ssh(self) -> DriverResult:
        """Parse ``show topology`` to get resource groups, port display names,
        and the authoritative mapping from REST API port numbers to CLI names.

        Returns DriverResult with data dict:
          cards: {card_num: {'serial': str, 'description': str,
                             'resource_groups': [{'number': int, 'label': str,
                                                  'mode': str, 'ports': [{'display': str, 'type': str, ...}]}]}}
          port_display_map: {(card_num, api_port_num): display_name}  (filled after correlate())
        """
        raw = self._ssh_run('show topology', timeout=12)
        if not raw or 'Card' not in raw:
            return DriverResult(error='show topology returned no data')
        topo = self._parse_show_topology(raw)
        if not topo.get('cards'):
            return DriverResult(error='No cards parsed from show topology')
        return DriverResult(success=True, data=topo)

    def _parse_show_topology(self, output: str) -> dict:
        """Parse the tree-formatted ``show topology`` output.

        Example lines::

            AresONE - Primary (ChassisSN MY25290004)
                `- Card 1 800GE-8P-OSFP-M+ROCEV2 (SN MY25290004)
                       +- Resource Group 01 (RG01)- 2x400GBASE-CR4 mode
                       |      +- Port 1.1 400GBASE-CR4 (owner) Link Up
        """
        cards: dict[int, dict] = {}
        cli_ports_by_card: dict[int, list[str]] = {}
        current_card: int | None = None
        current_rg: dict | None = None

        for line in output.splitlines():
            stripped = line.lstrip(' |`+-\t')

            card_m = re.match(r'Card\s+(\d+)\s+(.*?)(?:\s*\(SN\s+(\S+)\))?\s*$', stripped)
            if card_m:
                current_card = int(card_m.group(1))
                cards[current_card] = {
                    'description': card_m.group(2).strip(),
                    'serial': (card_m.group(3) or '').strip(),
                    'resource_groups': [],
                }
                cli_ports_by_card[current_card] = []
                current_rg = None
                continue

            rg_m = re.match(
                r'Resource Group\s+(\d+)\s+\(RG(\d+)\)-?\s*(.*?)(?:\s+mode)?\s*$',
                stripped,
            )
            if not rg_m:
                # XGS / older builds: "Resource Group 1 (RG1)- ..." or missing (RG) token
                rg_m = re.match(
                    r'Resource Group\s+(\d+)\s*(?:\(RG\d+\))?\s*-?\s*(.*?)(?:\s+mode)?\s*$',
                    stripped,
                )
            if rg_m and current_card is not None:
                rg_num = int(rg_m.group(1))
                if rg_m.lastindex and rg_m.lastindex >= 3:
                    mode_str = (rg_m.group(3) or '').strip()
                else:
                    mode_str = (rg_m.group(2) or '').strip()
                current_rg = {
                    'number': rg_num,
                    'label': f'RG{rg_num:02d}',
                    'mode': mode_str,
                    'ports': [],
                }
                cards[current_card]['resource_groups'].append(current_rg)
                continue

            port_m = re.match(
                r'Port\s+([\d.]+)\s+'           # port display name (1.1 or 3)
                r'(.+?)\s+'                     # type (greedy-minimal until last space group)
                r'Link\s+(\S+)',                # Link Up/Down
                stripped,
            )
            if port_m and current_card is not None:
                display = port_m.group(1)
                middle = port_m.group(2).strip()
                link = port_m.group(3)
                owner_m = re.search(r'\(([^)]*)\)\s*$', middle)
                if owner_m:
                    owner = owner_m.group(1)
                    ptype = middle[:owner_m.start()].strip()
                else:
                    owner = ''
                    ptype = middle
                entry = {'display': display, 'type': ptype,
                         'owner': owner, 'link': link}
                if current_rg is not None:
                    current_rg['ports'].append(entry)
                cli_ports_by_card[current_card].append(display)
                continue

            # Some IxOS chassis list "Port N ..." without the standard "Link Up" token on the same line.
            port_loose = re.match(r'Port\s+([\d.]+)\s+(.+)$', stripped)
            if port_loose and current_card is not None:
                display = port_loose.group(1)
                rest = port_loose.group(2).strip()
                link_m = re.search(r'Link\s*:\s*(\S+)|\bLink\s+(\S+)', rest)
                link = (link_m.group(1) or link_m.group(2)) if link_m else ''
                owner_m = re.search(r'\(([^)]*)\)\s*$', rest)
                if owner_m:
                    owner = owner_m.group(1)
                    ptype = rest[:owner_m.start()].strip()
                else:
                    owner = ''
                    ptype = rest
                entry = {'display': display, 'type': ptype,
                         'owner': owner, 'link': link or 'unknown'}
                if current_rg is not None:
                    current_rg['ports'].append(entry)
                cli_ports_by_card[current_card].append(display)

        # Classic XGS12/XM: ports under a card with no "Resource Group" headers — still show one group.
        for c_num, card_info in cards.items():
            if card_info.get('resource_groups'):
                continue
            displays = cli_ports_by_card.get(c_num) or []
            if not displays:
                continue
            card_info['resource_groups'] = [{
                'number': 1,
                'label': 'RG01',
                'mode': '',
                'ports': [
                    {'display': d, 'type': '', 'owner': '', 'link': ''}
                    for d in displays
                ],
            }]

        return {
            'cards': cards,
            'cli_ports_by_card': cli_ports_by_card,
        }

    def correlate_ports(self, topo_data: dict, rest_ports: list) -> dict:
        """Build (card_number, api_port_number) -> CLI display name mapping.

        Correlates REST ports (sorted by port_number per card) with CLI ports
        (in document order from ``show topology``).  If port counts per card
        do not match, falls back to identity mapping.
        """
        from collections import defaultdict
        api_by_card: dict[int, list[int]] = defaultdict(list)
        for p in rest_ports:
            api_by_card[p.get('card_number', 0)].append(p.get('port_number', 0))
        for v in api_by_card.values():
            v.sort()

        cli_by_card = topo_data.get('cli_ports_by_card', {})
        mapping: dict[tuple[int, int], str] = {}

        for card_num, api_ports in api_by_card.items():
            cli_ports = cli_by_card.get(card_num, [])
            if len(api_ports) == len(cli_ports):
                for api_pn, cli_display in zip(api_ports, cli_ports):
                    mapping[(card_num, api_pn)] = cli_display
            else:
                for api_pn in api_ports:
                    mapping[(card_num, api_pn)] = str(api_pn)

        return mapping

    # --- show lldp-peer-info ---------------------------------------------

    @staticmethod
    def _lldp_peer_info_disabled(output: str) -> bool:
        text = (output or '').lower()
        return (
            'lldp-peer-info status is disabled' in text
            or 'lldp-peer-info status: disabled' in text
        )

    def get_lldp_peer_info_status(self) -> DriverResult:
        """Return whether chassis ``lldp-peer-info`` is enabled (IxOS SSH)."""
        raw = self._ssh_run('show lldp-peer-info status', timeout=12) or ''
        if not raw.strip():
            return DriverResult(
                error=self._last_ssh_error or 'show lldp-peer-info status returned no data',
            )
        disabled = self._lldp_peer_info_disabled(raw)
        return DriverResult(
            success=True,
            data={
                'enabled': not disabled,
                'raw': raw.strip(),
            },
        )

    def _ssh_run_confirm(
        self,
        cmd: str,
        *,
        confirm: bool = True,
        timeout: int = 90,
    ) -> str:
        """Run an IxOS CLI command that may prompt ``[yes/NO]:`` (e.g. IxServer restart).

        Uses an interactive shell so we can answer the confirmation. ``confirm=False``
        sends ``no`` (useful for dry probes).
        """
        import paramiko

        self._last_ssh_error = ''
        base_cmd = (cmd or '').strip()
        if not base_cmd:
            return ''

        if self._needs_chassis_cli_prefix() and not base_cmd.lower().startswith('chassis '):
            run_cmds = [f'chassis {base_cmd}', base_cmd]
        else:
            run_cmds = [base_cmd]

        answer = 'yes\n' if confirm else 'no\n'
        for host in self._ssh_hosts():
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    host, username=self.username, password=self.password,
                    timeout=8, look_for_keys=False, allow_agent=False,
                )
                channel = client.invoke_shell(term='vt100', width=160, height=48)
                channel.settimeout(timeout)
                deadline = time.time() + timeout
                buf = ''

                def _drain(wait_s: float = 0.0) -> None:
                    nonlocal buf
                    if wait_s:
                        time.sleep(wait_s)
                    while channel.recv_ready():
                        buf += channel.recv(65536).decode('utf-8', errors='replace')

                # Wait for chassis CLI to finish auto-enter / banner.
                while time.time() < deadline:
                    _drain(0.4)
                    lower = buf.lower()
                    if 'admin@' in lower or 'ixos cli' in lower:
                        break

                last_out = ''
                for run_cmd in run_cmds:
                    before = len(buf)
                    channel.send(run_cmd + '\n')
                    answered = False
                    # Restart prompt often appears after a late banner; wait longer.
                    prompt_wait_until = time.time() + 25
                    while time.time() < deadline:
                        _drain(0.35)
                        chunk = buf[before:]
                        lower = chunk.lower()
                        if 'unknown or incomplete command' in lower:
                            break
                        if (
                            not answered
                            and ('[yes/no]' in lower or 'are you sure you want to restart' in lower)
                        ):
                            channel.send(answer)
                            answered = True
                            # Collect post-answer output briefly
                            _drain(1.5)
                            break
                        if not answered and time.time() > prompt_wait_until:
                            # No restart prompt — command may already be applied or unsupported
                            break
                    last_out = buf[before:]
                    if last_out and 'Unknown or incomplete command' not in last_out:
                        return last_out
                return last_out
            except Exception as e:
                self._last_ssh_error = f'{host}: {e}'
                logger.debug('IxOS SSH confirm %s cmd=%r failed: %s', host, base_cmd, e)
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        return ''

    def enable_lldp_peer_info(self, *, confirm_restart: bool = True) -> DriverResult:
        """Enable chassis LLDP peer-info via ``set lldp-peer-info enabled``.

        IxOS restarts IxServer when applying this change; ``confirm_restart`` must
        be True to accept the interactive restart prompt.
        """
        status = self.get_lldp_peer_info_status()
        if status.success and isinstance(status.data, dict) and status.data.get('enabled'):
            return DriverResult(success=True, data={'already_enabled': True, 'raw': status.data.get('raw', '')})

        raw = self._ssh_run_confirm(
            'set lldp-peer-info enabled',
            confirm=confirm_restart,
            timeout=120,
        )
        if not raw:
            return DriverResult(error=self._last_ssh_error or 'set lldp-peer-info enabled returned no data')
        lower = raw.lower()
        if 'unknown or incomplete command' in lower:
            return DriverResult(
                error='set lldp-peer-info enabled not supported on this chassis',
                data={'raw': raw},
            )
        if not confirm_restart:
            return DriverResult(
                success=False,
                error='IxServer restart not confirmed',
                data={'raw': raw},
            )
        # Detect explicit abort after prompt (we answered no, or operator rejected).
        if '[yes/no]' in lower:
            after = lower.split('[yes/no]', 1)[-1]
            # First non-empty token after prompt
            token = after.replace(':', ' ').strip().split(None, 1)
            if token and token[0] in ('no', 'n'):
                return DriverResult(error='IxServer restart was not accepted', data={'raw': raw})

        # IxServer restart can take several minutes; wait for chassis READY, then confirm status.
        enabled = False
        last_status = ''
        chassis_ready = False
        for _ in range(36):  # up to ~3 minutes
            time.sleep(5)
            st_raw = self._ssh_run('show chassis status', timeout=12) or ''
            if 'READY' in st_raw.upper() and 'BOOTING' not in st_raw.upper():
                chassis_ready = True
            st = self.get_lldp_peer_info_status()
            if st.success and isinstance(st.data, dict):
                last_status = st.data.get('raw') or ''
                if st.data.get('enabled'):
                    enabled = True
                    if chassis_ready:
                        break

        if enabled:
            return DriverResult(
                success=True,
                data={
                    'already_enabled': False,
                    'status': last_status,
                    'chassis_ready': chassis_ready,
                    'raw': raw,
                },
            )
        return DriverResult(
            success=False,
            error='set lldp-peer-info enabled ran but status is still disabled (IxServer may still be restarting)',
            data={'status': last_status, 'chassis_ready': chassis_ready, 'raw': raw},
        )

    def enable_lldp(self) -> DriverResult:
        """Alias for topology / management-command parity with switch drivers."""
        return self.enable_lldp_peer_info(confirm_restart=True)

    def get_lldp_ssh(
        self,
        bps_topology: dict | None = None,
        chassis_type: str = '',
    ) -> DriverResult:
        """Fetch LLDP neighbors via SSH ``show lldp-peer-info data``.

        ``bps_topology`` / ``chassis_type`` are accepted for caller parity with
        KCOSDriver.get_lldp_ssh (M8400 path); IxOS parsing does not use them.

        Returns list of {local_port, remote_device, remote_port, chassis_id, mgmt_ip}.
        """
        raw = self._ssh_run('show lldp-peer-info data', timeout=12)
        if not raw:
            return DriverResult(error='SSH lldp-peer-info returned no data')
        if self._lldp_peer_info_disabled(raw):
            return DriverResult(
                error=(
                    'lldp-peer-info is disabled on this chassis '
                    '(enable with: set lldp-peer-info enabled — restarts IxServer)'
                ),
                data=[],
            )
        lower = raw.lower()
        if (
            'non-zero exit status' in lower
            or 'returned non-zero exit status' in lower
            or ('ixserverctl' in lower and 'error' in lower)
        ):
            return DriverResult(
                error='IxServer LLDP query failed (chassis may still be restarting after enable)',
                data=[],
            )
        neighbors = self._parse_lldp_peer_info(raw)
        return DriverResult(success=True, data=neighbors)

    @staticmethod
    def _parse_lldp_peer_info(output: str) -> list[dict]:
        """Parse ``show lldp-peer-info data`` output.

        Format::

            Port 1.1
                System MAC: A8:27:C8:4A:15:40
                Port ID: Eth57/1(Port57)
                System name: sonic
                System IP: 192.0.2.10
                Port description: Ethernet448
        """
        neighbors: list[dict] = []
        current: dict | None = None

        for line in output.splitlines():
            stripped = line.strip()
            if not stripped or stripped.lower().startswith('chassis peer'):
                continue
            port_m = re.match(r'^Port\s+([\d.]+)\s*$', stripped, re.I)
            card_port_m = re.match(r'^Card\s+(\d+)\s+Port\s+(\d+)\s*$', stripped, re.I)
            if port_m or card_port_m:
                if current and current.get('local_port'):
                    neighbors.append(current)
                if card_port_m:
                    cn, pn = card_port_m.group(1), card_port_m.group(2)
                    local_port = f'{cn}/{pn}'
                else:
                    local_port = port_m.group(1)
                current = {
                    'local_port': local_port,
                    'remote_device': '',
                    'remote_port': '',
                    'chassis_id': '',
                    'mgmt_ip': '',
                }
                continue
            if current is None:
                continue
            if ':' not in stripped:
                continue
            key, _, val = stripped.partition(':')
            key = key.strip().lower()
            val = val.strip()
            if key == 'system name':
                current['remote_device'] = val
            elif key == 'system ip':
                current['mgmt_ip'] = val
            elif key == 'system mac':
                current['chassis_id'] = val
            elif key == 'port id':
                current['remote_port'] = val
            elif key == 'port description':
                if not current['remote_port'] or current['remote_port'].startswith('Eth'):
                    current['remote_port'] = val

        if current and current.get('local_port') and (
            current.get('remote_device') or current.get('mgmt_ip')
        ):
            neighbors.append(current)

        return neighbors

    # ------------------------------------------------------------------
    # Public API - services
    # ------------------------------------------------------------------

    def get_services(self) -> DriverResult:
        """Fetch running services on the chassis."""
        return self._get('/services')

    # ------------------------------------------------------------------
    # Public API - port operations
    # ------------------------------------------------------------------

    def take_ownership(self, port_id: int) -> DriverResult:
        """Take ownership of a port."""
        return self._post_operation(f'/ports/{port_id}/operations/takeownership')

    def release_ownership(self, port_id: int) -> DriverResult:
        """Release ownership of a port."""
        return self._post_operation(f'/ports/{port_id}/operations/releaseownership')

    def reboot_port(self, port_id: int) -> DriverResult:
        """Reboot a port."""
        return self._post_operation(f'/ports/{port_id}/operations/reboot')

    def reset_port(self, port_id: int) -> DriverResult:
        """Reset a port to factory defaults."""
        return self._post_operation(f'/ports/{port_id}/operations/resetfactorydefaults')

    # ------------------------------------------------------------------
    # Public API - card operations
    # ------------------------------------------------------------------

    def hotswap_card(self, card_id: int) -> DriverResult:
        """Hotswap a card."""
        return self._post_operation(f'/cards/{card_id}/operations/hotswap')

    # ------------------------------------------------------------------
    # Public API - upgrade / operations
    # ------------------------------------------------------------------

    def get_available_versions(self) -> DriverResult:
        """List IxOS versions available on the chassis."""
        return self._get('/chassis')  # versions are in ixosApplications

    def upgrade_chassis(self, version: str) -> DriverResult:
        """Initiate an IxOS upgrade (async with polling)."""
        # Get chassis ID first
        result = self._get('/chassis')
        if not result.success:
            return result
        raw = result.data
        if isinstance(raw, list) and raw:
            raw = raw[0]
        chassis_id = raw.get('id', 1) if isinstance(raw, dict) else 1
        return self._post_operation(
            f'/chassis/{chassis_id}/operations/upgrade',
            timeout=300
        )

    def get_chassis_operations(self) -> DriverResult:
        """List available/running operations on the chassis."""
        result = self._get('/chassis')
        if not result.success:
            return result
        raw = result.data
        if isinstance(raw, list) and raw:
            raw = raw[0]
        chassis_id = raw.get('id', 1) if isinstance(raw, dict) else 1
        return self._get(f'/chassis/{chassis_id}/operations')

    # Stubs to match KCOS interface
    def get_snapshots(self) -> DriverResult:
        """IxOS does not have snapshot management."""
        return DriverResult(success=True, data=[])

    def create_snapshot(self, label: str) -> DriverResult:
        return DriverResult(error='Snapshots not supported on IxOS')

    def restore_snapshot(self, name: str) -> DriverResult:
        return DriverResult(error='Snapshots not supported on IxOS')

    def delete_snapshot(self, name: str) -> DriverResult:
        return DriverResult(error='Snapshots not supported on IxOS')

    # ------------------------------------------------------------------
    # Public API - licensing
    # ------------------------------------------------------------------

    def get_licenses(self) -> DriverResult:
        """Fetch license information."""
        try:
            # Step 1: get license servers
            resp, servers = self._request('GET',
                f'https://{self.ip}/platform/api/v2/licensing/servers')
            if resp is None or resp.status_code != 200:
                return DriverResult(error='Cannot reach licensing API')
            if not isinstance(servers, list) or not servers:
                return DriverResult(success=True, data=[])

            server_id = servers[0].get('id', 1)

            # Step 2: trigger license retrieval (async)
            retrieve_url = f'https://{self.ip}/platform/api/v2/licensing/servers/{server_id}/operations/retrievelicenses'
            resp2, data2 = self._request('POST', retrieve_url, payload='')
            if resp2 is None:
                return DriverResult(error='License retrieval request failed')

            # Step 3: get result (may be direct or via resultUrl)
            if resp2.status_code == 202 and isinstance(data2, str) and data2.startswith('http'):
                resp3, licenses = self._request('GET', data2)
            elif resp2.status_code == 200:
                licenses = data2
            else:
                # Try direct result endpoint
                result_url = f'https://{self.ip}/platform/api/v2/licensing/servers/{server_id}/operations/retrievelicenses/1/result'
                resp3, licenses = self._request('GET', result_url)

            if isinstance(licenses, list):
                return DriverResult(success=True, data=licenses)
            return DriverResult(success=True, data=[])
        except Exception as e:
            return DriverResult(error=str(e))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_link(raw_state: str, owner: str = '') -> tuple[str, str]:
        """Normalize IxOS linkState to a simple (state, led_color) pair.
        LED colors follow the LabVault convention:
          green  = link up
          red    = link down (transceiver present)
          yellow = administratively down / owned but not up
          amber  = loopback or special state
          off    = no transceiver / empty
        """
        low = raw_state.lower()
        if low in ('linkup', 'up'):
            return 'up', 'green'
        if low in ('linkdown', 'down'):
            if owner and owner != 'Free':
                return 'down', 'yellow'   # owned but down
            return 'down', 'red'
        if low in ('notransceiver', 'no_transceiver'):
            return 'noTransceiver', 'off'
        if low in ('loopback',):
            return 'loopback', 'amber'
        if low in ('admindisabled', 'disabled'):
            return 'disabled', 'yellow'
        # Anything else
        return raw_state, 'off'

    @staticmethod
    def format_bytes(size_bytes: int) -> str:
        """Human-readable byte sizes."""
        if size_bytes == 0:
            return '0 B'
        units = ('B', 'KB', 'MB', 'GB', 'TB')
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f'{s} {units[i]}'
