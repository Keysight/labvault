"""
BPS (BreakingPoint) REST API Driver for Keysight APS chassis.

Authentication : BPS native session auth
                 POST /bps/api/v1/auth/session  with JSON {username, password}
                 Returns sessionId + apiKey used as request headers.

This driver is intentionally lightweight - it only exposes the
read-only topology / slot information needed by LabVault.
"""
from __future__ import annotations

import logging
import time
import threading
import requests
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

logger = logging.getLogger(__name__)

from .ixos import DriverResult
from ..ip_addressing import bracket_host


# ---------------------------------------------------------------------------
# Mode / fanout lookup tables (mirror bps_pythar.py)
# ---------------------------------------------------------------------------

BPS_MODES = {
    7: 'BreakingPoint',
    10: 'BreakingPoint L2/L3',
    3: 'IxLoad',
    12: 'BreakingPoint QT',
}

BPS_FANOUTS = {
    0: '100G', 1: '40G', 2: '25G', 3: '10G', 4: '50G',
}

BPS_MODE_LABELS = {v: k for k, v in BPS_MODES.items()}


# ---------------------------------------------------------------------------
# BPS Session cache — reuse auth across driver instances for same IP
# ---------------------------------------------------------------------------
_bps_auth_cache: dict[str, dict] = {}   # ip -> {session, expires}
_bps_auth_lock = threading.Lock()
_BPS_AUTH_TTL = 300  # 5 min


def _get_cached_session(ip: str) -> tuple[requests.Session | None, bool]:
    """Return (session, is_authenticated) from cache if still valid."""
    with _bps_auth_lock:
        entry = _bps_auth_cache.get(ip)
        if entry and time.time() < entry.get('expires', 0):
            return entry['session'], True
    return None, False


def _cache_session(ip: str, session: requests.Session):
    with _bps_auth_lock:
        _bps_auth_cache[ip] = {
            'session': session,
            'expires': time.time() + _BPS_AUTH_TTL,
        }


# ---------------------------------------------------------------------------
# Topology URL candidates
# ---------------------------------------------------------------------------
_TOPOLOGY_PATHS = [
    '/bps/api/v2/sessions/0/bps/topology',
    '/bps/api/v2/sessions/1/bps/topology',
    '/bps/api/v2/core/topology',
]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class BPSDriver:
    """Lightweight driver for BPS REST API on KCOS chassis."""

    _SLOT_BASE = '/bps/api/v2/sessions/0/bps/topology'

    def __init__(self, ip: str, username: str = 'admin', password: str = 'admin'):
        self.ip = bracket_host((ip or '').strip())
        self.username = username
        self.password = password
        self._topo_base: str = ''

        # Try reusing a cached session first
        cached_sess, authed = _get_cached_session(ip)
        if authed and cached_sess:
            self._session = cached_sess
            self._authenticated = True
        else:
            self._session = requests.Session()
            self._session.verify = False
            self._authenticated = False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _login(self) -> bool:
        if self._authenticated:
            return True
        try:
            resp = self._session.post(
                f'https://{self.ip}/bps/api/v1/auth/session',
                json={'username': self.username, 'password': self.password},
                headers={'Content-Type': 'application/json'},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                sid = data.get('sessionId', '')
                key = data.get('apiKey', '')
                if sid and key:
                    self._session.headers.update({'sessionId': sid, 'X-API-KEY': key})
                    self._authenticated = True
                    _cache_session(self.ip, self._session)
                    return True
            logger.warning('BPS auth failed for %s: HTTP %s', self.ip, resp.status_code)
            return False
        except Exception as exc:
            logger.warning('BPS auth error for %s: %s', self.ip, exc)
            return False

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, timeout: int = 10) -> DriverResult:
        if not self._login():
            return DriverResult(error='BPS authentication failed')
        try:
            resp = self._session.get(url, headers={'Content-Type': 'application/json'}, timeout=timeout)
            if resp.status_code == 200:
                try:
                    return DriverResult(success=True, data=resp.json())
                except ValueError:
                    return DriverResult(error=f'BPS: invalid JSON from {url}')
            return DriverResult(error=f'BPS: HTTP {resp.status_code} from {url}')
        except Exception as exc:
            return DriverResult(error=f'BPS: {exc}')

    def _get_first_success(self, paths: list[str], timeout: int = 10) -> DriverResult:
        if not self._login():
            return DriverResult(error='BPS authentication failed')
        last_err = ''
        hdrs = {'Content-Type': 'application/json'}
        for path in paths:
            url = f'https://{self.ip}{path}'
            try:
                resp = self._session.get(url, headers=hdrs, timeout=timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    self._topo_base = path
                    return DriverResult(success=True, data=data)
                last_err = f'{path}: HTTP {resp.status_code}'
            except Exception as exc:
                last_err = f'{path}: {exc}'
        return DriverResult(error=f'BPS topology not found. Last: {last_err}')

    # ------------------------------------------------------------------
    # Topology — single API call, parse everything from the response
    # ------------------------------------------------------------------

    def get_topology(self) -> DriverResult:
        """Fetch BPS topology in a single API call.

        The main topology response already embeds port[] and fpga[] per slot,
        so we parse everything from that one response — no per-slot calls needed.
        """
        result = self._get_first_success(_TOPOLOGY_PATHS)
        if not result.success:
            return result

        raw = result.data
        slots_raw = raw.get('slot', []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])

        slots = []
        for s in slots_raw:
            if not isinstance(s, dict):
                continue
            slot_id = s.get('id', 0)
            try:
                slot_id = int(slot_id)
            except (ValueError, TypeError):
                continue
            if slot_id == 0:
                continue

            model = s.get('model', '')
            mode_raw = s.get('mode', '')
            supports_l23 = s.get('supportsL23', False)

            # Resolve mode
            mode_id = -1
            mode_label = str(mode_raw)
            if isinstance(mode_raw, int) or (isinstance(mode_raw, str) and mode_raw.isdigit()):
                mode_id = int(mode_raw)
                mode_label = BPS_MODES.get(mode_id, f'Mode {mode_id}')
            else:
                mode_id = BPS_MODE_LABELS.get(mode_raw, -1)
            is_l23 = (mode_id == 10 or 'l2/l3' in mode_label.lower())

            # --- Parse ports from embedded data (no extra API call) ---
            ports_raw = s.get('port', [])
            if not isinstance(ports_raw, list):
                ports_raw = []

            ports = []
            physical_ports: dict[str, dict] = {}

            for p in ports_raw:
                if not isinstance(p, dict):
                    continue
                try:
                    p_speed = int(p.get('speed', 0))
                except (ValueError, TypeError):
                    p_speed = 0

                pid = str(p.get('id', '0'))
                parts = pid.split('.')
                phys_num = parts[0] if parts else pid
                lane = parts[1] if len(parts) > 1 else '0'

                # Transceiver
                xcvr_raw = p.get('transceiver')
                if isinstance(xcvr_raw, dict):
                    xcvr_vendor = xcvr_raw.get('vendor') or ''
                    xcvr_part = xcvr_raw.get('vendorPartNumber') or ''
                    xcvr_display = f'{xcvr_vendor} {xcvr_part}'.strip() if xcvr_vendor else ''
                else:
                    xcvr_vendor = xcvr_part = xcvr_display = ''

                port_entry = {
                    'id': pid,
                    'physical_port': int(phys_num) if phys_num.isdigit() else 0,
                    'lane': int(lane) if lane.isdigit() else 0,
                    'speed': p_speed,
                    'speed_display': self._fmt_speed(p_speed),
                    'link': str(p.get('link', 'down')),
                    'media': str(p.get('media', '')),
                    'currentMode': str(p.get('currentMode', '')),
                    'possibleModes': str(p.get('possibleModes', '')),
                    'transceiver_display': xcvr_display,
                    'reservedBy': str(p.get('reservedBy', '')),
                    'owner': str(p.get('owner', '')),
                }
                ports.append(port_entry)

                # Group by physical port
                if phys_num not in physical_ports:
                    physical_ports[phys_num] = {
                        'id': int(phys_num) if phys_num.isdigit() else 0,
                        'currentMode': port_entry['currentMode'],
                        'possibleModes': port_entry['possibleModes'],
                        'transceiver_display': xcvr_display,
                        'reservedBy': port_entry['reservedBy'],
                        'media': port_entry['media'],
                        'lanes': [],
                        'links_up': 0,
                        'total_lanes': 0,
                    }
                pg = physical_ports[phys_num]
                pg['lanes'].append(port_entry)
                pg['total_lanes'] += 1
                if port_entry['link'] in ('up', 'UP'):
                    pg['links_up'] += 1

            # Compute per-physical-port summaries
            phys_list = sorted(physical_ports.values(), key=lambda x: x['id'])
            for pp in phys_list:
                lanes = pp['lanes']
                ls = lanes[0].get('speed', 0) if lanes else 0
                pp['lane_speed_display'] = self._fmt_speed(ls)
                pp['total_bw_display'] = self._fmt_speed(ls * len(lanes))

            # --- Parse FPGA from embedded data (no extra API call) ---
            fpga_raw = s.get('fpga', [])
            fpga_engines = []
            if isinstance(fpga_raw, list):
                for eng in fpga_raw:
                    if isinstance(eng, dict):
                        fpga_engines.append({
                            'id': eng.get('id', 0),
                            'name': eng.get('name', ''),
                            'state': eng.get('state', ''),
                            'resourceType': eng.get('resourceType', ''),
                            'reservedBy': eng.get('reservedBy', ''),
                        })

            fanout = phys_list[0]['currentMode'] if phys_list else 'N/A'

            slots.append({
                'id': slot_id,
                'model': model,
                'mode': mode_label,
                'mode_id': mode_id,
                'is_l23': is_l23,
                'supports_l23': supports_l23,
                'ports': ports,
                'physical_ports': phys_list,
                'fpga_engines': fpga_engines,
                'fanout': fanout,
                'port_count': len(ports),
                'physical_port_count': len(phys_list),
            })

        slots.sort(key=lambda x: x['id'])
        logger.info('BPS topology for %s: %d slots, ports=%s, fpga=%s',
                     self.ip, len(slots),
                     [s['port_count'] for s in slots],
                     [len(s['fpga_engines']) for s in slots])
        return DriverResult(success=True, data={'slots': slots, 'raw': raw})

    # ------------------------------------------------------------------
    # Public helpers for type detection
    # ------------------------------------------------------------------

    def get_chassis_model(self) -> DriverResult:
        """Get model from topology raw (e.g. 'APS-M8400')."""
        result = self._get_first_success(_TOPOLOGY_PATHS)
        if result.success and isinstance(result.data, dict):
            return DriverResult(success=True, data={'model': result.data.get('model', '')})
        return DriverResult(success=True, data={'model': ''})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_speed(speed: int) -> str:
        if speed >= 1000000:
            return f'{speed // 1000000}Tbps'
        if speed >= 1000:
            return f'{speed // 1000}G'
        return f'{speed}M' if speed else ''
