"""
FortiGate driver - communicates via FortiOS REST API (HTTPS).
Supports both API token auth and session-based auth (username/password).
VDOM-aware: enumerates all VDOMs and fetches interfaces/policies/routes per VDOM.
Handles FortiOS 7.6+ (Angular SPA with /api/v2/authentication) and legacy versions
(/logincheck with APSCOOKIE).

Auth: when ``Device.api_key`` is set it is sent as the ``access_token`` query
parameter on every request and no login is attempted. Otherwise a per-IP cookie
session is created (``/logincheck`` first, then ``/api/v2/authentication``), reused
for ``_SESSION_TTL`` seconds, and re-authenticated once on HTTP 401/403. A
``LOCKED_OUT`` reply suppresses further login attempts for 5 minutes. HTTPS only,
``api_port`` default 443, TLS verification disabled, first connect target only.
"""
import re
import time
import logging
import requests
from urllib3.exceptions import InsecureRequestWarning

from .base import BaseDriver, DriverResult

logger = logging.getLogger(__name__)
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

_sessions = {}
_vdom_cache = {}  # ip -> [vdom_names]
_lockout_until = {}  # ip -> timestamp — avoid hammering locked-out devices
_auth_time = {}  # ip -> timestamp of last successful auth

# How long to consider a session valid before re-validating (seconds).
# FortiGate default admin session timeout is 480s (8 min).  Stay well under.
_SESSION_TTL = 300  # 5 minutes


def _get_session(ip):
    if ip not in _sessions:
        s = requests.Session()
        s.verify = False
        _sessions[ip] = s
    return _sessions[ip]


def _logout(ip):
    """Gracefully log out from FortiGate to free the admin session slot."""
    session = _sessions.get(ip)
    if session and getattr(session, '_fg_authed', False):
        try:
            port = 443
            base = f"https://{ip}:{port}"
            session.post(f"{base}/logout", timeout=5)
            logger.debug('FortiGate [%s]: logged out successfully', ip)
        except Exception:
            pass
        session._fg_authed = False
    _auth_time.pop(ip, None)


def clear_cache(ip):
    """Log out (network call) and drop session, VDOM list and lockout state for *ip*."""
    _logout(ip)  # free the session on the device first
    _sessions.pop(ip, None)
    _vdom_cache.pop(ip, None)
    _lockout_until.pop(ip, None)


class FortiGateDriver(BaseDriver):
    """FortiGate firewall driver over FortiOS REST (``/api/v2/monitor`` + ``/api/v2/cmdb``).

    VDOM-aware collectors (interfaces, routes, ARP, LLDP, policies, VPN, BGP, OSPF)
    loop over :meth:`get_vdoms` and tag rows with ``vdom``.
    """

    VENDOR_NAME = 'fortigate'

    def _base_url(self):
        port = self.api_port or 443
        return f"https://{self.ip}:{port}"

    def _extract_csrf(self, session):
        """Extract CSRF token from session cookies and set it in headers.
        Handles both legacy (ccsrftoken) and 7.6+ (ccsrf_token_443_<hash>) names.
        Returns True if a CSRF cookie was found."""
        for cookie in session.cookies:
            if 'csrf' in cookie.name.lower():
                session.headers['X-CSRFTOKEN'] = cookie.value.strip('"')
                return True
        return False

    def _ensure_auth(self, session):
        """Return True when *session* can make API calls (token mode or fresh login)."""
        if self.api_key:
            return True
        if hasattr(session, '_fg_authed') and session._fg_authed:
            # Session already authenticated — check if it's still fresh
            auth_ts = _auth_time.get(self.ip, 0)
            if auth_ts and (time.time() - auth_ts) < _SESSION_TTL:
                return True
            # Session may have expired on the device — mark stale but don't
            # logout yet (the _get method will handle 401 retry).
            logger.debug('FortiGate [%s]: session older than %ds, will re-validate on next API call', self.ip, _SESSION_TTL)
            session._fg_authed = False

        # ---- Lockout guard: skip login attempts while device is locked out ----
        lockout_ts = _lockout_until.get(self.ip, 0)
        if lockout_ts and time.time() < lockout_ts:
            remaining = int(lockout_ts - time.time())
            logger.info('FortiGate [%s]: skipping login — lockout expires in %ds', self.ip, remaining)
            return False

        base = self._base_url()

        # ---------- Strategy 1: Legacy /logincheck (works on ALL versions) ----------
        # Try this first — it's the most reliable and works on both old and new firmware.
        try:
            resp = session.post(
                f"{base}/logincheck",
                data={'username': self.username, 'secretkey': self.password},
                timeout=10,
            )
            if resp.status_code == 200:
                if self._extract_csrf(session):
                    session._fg_authed = True
                    _auth_time[self.ip] = time.time()
                    logger.info('FortiGate [%s]: authenticated via /logincheck', self.ip)
                    # Handle post-login-banner (FortiOS 7.x feature)
                    try:
                        session.post(f"{base}/logindisclaimer", data='confirm=1', timeout=5)
                        self._extract_csrf(session)
                    except Exception:
                        pass
                    return True
                else:
                    # No CSRF cookie after logincheck.
                    # On FortiOS 7.6+ the logincheck returns the Angular SPA page
                    # (>500 bytes) instead of a short redirect — fall through to Strategy 2.
                    if len(resp.text) > 500:
                        logger.debug(
                            'FortiGate [%s]: logincheck returned SPA page — '
                            'trying FortiOS 7.6+ JSON auth', self.ip)
                    else:
                        # Old firmware, genuinely bad credentials
                        logger.warning(
                            'FortiGate [%s]: logincheck returned 200 but '
                            'no CSRF cookie — bad credentials', self.ip)
                        return False
        except requests.exceptions.ConnectionError:
            logger.debug('FortiGate [%s]: cannot connect', self.ip)
            return False
        except Exception as e:
            logger.debug('FortiGate [%s]: /logincheck error: %s', self.ip, e)

        # ---------- Strategy 2: FortiOS 7.6+ JSON authentication ----------
        # Only reached if logincheck returned the Angular SPA page (no cookies).
        # The 7.6 Angular SPA uses /api/v2/authentication with JSON payload.
        try:
            # Pre-fetch login page to get initial session/CSRF cookies
            session.cookies.clear()
            session.get(f"{base}/login", timeout=8)
            csrf_header = {}
            for cookie in session.cookies:
                if 'csrf' in cookie.name.lower():
                    csrf_header['X-CSRFTOKEN'] = cookie.value.strip('"')
                    break

            # FortiOS 7.6 uses 'username' + 'password' (not 'secretkey')
            resp = session.post(
                f"{base}/api/v2/authentication",
                json={'username': self.username, 'password': self.password},
                headers={**csrf_header, 'Content-Type': 'application/json'},
                timeout=10,
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    status_msg = data.get('status_message', '')
                    err_code = data.get('error', 0)
                    err_msg = data.get('error_message', '')

                    if status_msg == 'LOGIN_SUCCESS':
                        self._extract_csrf(session)
                        session._fg_authed = True
                        _auth_time[self.ip] = time.time()
                        _lockout_until.pop(self.ip, None)
                        logger.info('FortiGate [%s]: authenticated via /api/v2/authentication (7.6+)', self.ip)
                        return True
                    elif err_msg == 'LOCKED_OUT' or err_code == 16:
                        _lockout_until[self.ip] = time.time() + 300
                        logger.warning(
                            'FortiGate [%s]: account LOCKED OUT — '
                            'will not retry for 5 minutes', self.ip)
                        return False
                    elif status_msg == 'LOGIN_FAILED':
                        logger.warning(
                            'FortiGate [%s]: /api/v2/authentication LOGIN_FAILED '
                            '(error=%s, %s)', self.ip, err_code, err_msg)
                        return False
                except (ValueError, KeyError):
                    pass
        except Exception as e:
            logger.debug('FortiGate [%s]: /api/v2/authentication error: %s', self.ip, e)

        return False

    def _get(self, path, params=None, timeout=10):
        """Authenticated GET; returns a ``requests.Response`` (synthetic 401 if login fails)."""
        session = _get_session(self.ip)
        if not self._ensure_auth(session):
            # Return a mock 401 response so callers can handle it
            resp = requests.models.Response()
            resp.status_code = 401
            resp._content = b'{"error": "authentication failed"}'
            return resp
        url = f"{self._base_url()}{path}"
        all_params = dict(params or {})
        if self.api_key:
            all_params['access_token'] = self.api_key
        resp = session.get(url, params=all_params, timeout=timeout)
        # Retry auth once on 401/403 (session may have expired on device)
        if resp.status_code in (401, 403) and not self.api_key:
            logger.info('FortiGate [%s]: got HTTP %d, re-authenticating', self.ip, resp.status_code)
            # Logout first to free the old session slot on the device
            _logout(self.ip)
            session.cookies.clear()
            if self._ensure_auth(session):
                resp = session.get(url, params=all_params, timeout=timeout)
        return resp

    def probe(self):
        """Probe FortiGate reachability and authentication.
        Reuses existing session to avoid burning admin session slots.
        Returns: 'ok', 'auth_failed', or 'unreachable'."""
        try:
            # Use _get which handles auth reuse and retry internally
            resp = self._get('/api/v2/monitor/system/status', timeout=8)
            if resp.status_code == 200:
                return 'ok'
            elif resp.status_code in (401, 403):
                return 'auth_failed'
            return 'unreachable'
        except requests.exceptions.SSLError:
            return 'unreachable'
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            return 'unreachable'
        except Exception as e:
            logger.debug(f"FortiGate probe error: {e}")
            return 'unreachable'

    # ---- VDOM enumeration ----

    def get_vdoms(self):
        """Enumerate all VDOMs on this FortiGate."""
        if self.ip in _vdom_cache:
            return _vdom_cache[self.ip]
        vdoms = ['root']  # default
        try:
            resp = self._get('/api/v2/cmdb/system/vdom')
            if resp.status_code == 200:
                results = resp.json().get('results', [])
                if results:
                    vdoms = [v.get('name', 'root') for v in results if v.get('name')]
        except Exception as e:
            logger.debug(f"FortiGate VDOM enumeration error: {e}")
        _vdom_cache[self.ip] = vdoms
        return vdoms

    def get_system_info(self):
        try:
            resp = self._get('/api/v2/monitor/system/status')
            if resp.status_code == 200:
                raw = resp.json()
                data = raw.get('results', raw)
                vdoms = self.get_vdoms()
                return DriverResult(success=True, data={
                    'hostname': data.get('hostname', self.ip),
                    'version': data.get('version', ''),
                    'serial_number': data.get('serial', ''),
                    'model_name': data.get('model_name', data.get('model', 'FortiGate')),
                    'mac_address': '',
                    'uptime': data.get('uptime', None),
                    'vendor_detail': f"FortiOS {data.get('version', '')} build {data.get('build', '')} | VDOMs: {', '.join(vdoms)}",
                })
            return DriverResult(error=f'HTTP {resp.status_code}')
        except Exception as e:
            return DriverResult(error=str(e))

    def get_interfaces(self):
        """Fetch interfaces from ALL VDOMs using both CMDB and monitor APIs.
        CMDB gives config (type, vdom, speed setting, ip, alias).
        Monitor gives live status (link state, actual speed, rx/tx bytes).
        We merge both for complete info."""
        vdoms = self.get_vdoms()
        seen = set()
        physical, vlans, tunnels, mgmt = [], [], [], []

        # 1) Fetch CMDB config for ALL interfaces (global scope)
        cmdb_map = {}  # name -> cmdb dict
        try:
            resp = self._get('/api/v2/cmdb/system/interface')
            if resp.status_code == 200:
                for intf in resp.json().get('results', []):
                    cmdb_map[intf.get('name', '')] = intf
        except Exception:
            pass

        # 2) Fetch live status per VDOM
        for vdom in vdoms:
            try:
                resp = self._get('/api/v2/monitor/system/interface', params={'vdom': vdom})
                if resp.status_code != 200:
                    continue
                results = resp.json().get('results', [])
                if isinstance(results, dict):
                    # Some FG versions return dict keyed by intf name
                    results = list(results.values())
                for intf in results:
                    name = intf.get('name', '')
                    if not name or name in seen:
                        continue
                    seen.add(name)

                    # Merge with CMDB
                    cmdb = cmdb_map.get(name, {})

                    # Link state
                    link = 'up' if intf.get('link', False) else 'down'
                    # Admin status from CMDB 'status' field
                    cmdb_status = cmdb.get('status', 'up')
                    admin = 'up' if cmdb_status in ('up', 'enable') else 'disabled'

                    # Speed: prefer monitor speed, fallback to CMDB configured speed
                    speed = intf.get('speed', 0)
                    if isinstance(speed, str):
                        speed = self._parse_fg_speed(speed)
                    elif isinstance(speed, (int, float)):
                        speed = int(speed)
                    else:
                        speed = 0
                    if speed == 0:
                        speed = self._parse_fg_speed(str(cmdb.get('speed', '')))
                    bw = speed * 1_000_000 if speed else 0

                    # IP address
                    ip_addr = intf.get('ip', cmdb.get('ip', ''))
                    if isinstance(ip_addr, list):
                        ip_addr = '.'.join(str(x) for x in ip_addr) if ip_addr else ''
                    mask = intf.get('netmask', cmdb.get('netmask', ''))
                    if isinstance(mask, list):
                        mask = '.'.join(str(x) for x in mask) if mask else ''
                    ip_display = f"{ip_addr}/{mask}" if ip_addr and mask and ip_addr != '0.0.0.0' else ''

                    # Interface type from CMDB
                    intf_type = cmdb.get('type', '')

                    # VDOM from CMDB or current iteration
                    intf_vdom = cmdb.get('vdom', [{}])
                    if isinstance(intf_vdom, list) and intf_vdom:
                        intf_vdom = intf_vdom[0].get('name', vdom) if isinstance(intf_vdom[0], dict) else str(intf_vdom[0])
                    elif isinstance(intf_vdom, str):
                        pass
                    else:
                        intf_vdom = vdom

                    # Build description parts
                    desc_parts = []
                    alias = cmdb.get('alias', intf.get('alias', ''))
                    if alias:
                        desc_parts.append(alias)
                    if ip_display:
                        desc_parts.append(ip_display)
                    if len(vdoms) > 1 and intf_vdom:
                        desc_parts.append(f"vdom:{intf_vdom}")
                    if intf_type:
                        desc_parts.append(intf_type)
                    description = ' | '.join(desc_parts)

                    # Media/transceiver info
                    media = intf.get('media', cmdb.get('media', ''))

                    entry = {
                        'name': name,
                        'short_name': self._short_name(name),
                        'status': link, 'admin_status': admin,
                        'description': description,
                        'status_color': self._status_color(link, admin),
                        'bandwidth': bw, 'speed_label': self._speed_label(bw),
                        'mtu': intf.get('mtu', cmdb.get('mtu', '')),
                        'mac': intf.get('mac', intf.get('macaddr', cmdb.get('macaddr', ''))),
                        'ip': ip_display,
                        'vdom': intf_vdom,
                        'type': intf_type,
                        'media': str(media),
                    }
                    lower = name.lower()
                    if intf_type in ('vlan', 'vlan-sub'):
                        vlans.append(entry)
                    elif '.' in name and intf_type not in ('physical', 'hard-switch'):
                        vlans.append(entry)
                    elif intf_type in ('tunnel', 'ipsec') or any(x in lower for x in ('tunnel', 'vpn', 'ssl.', 'ipsec')):
                        tunnels.append(entry)
                    elif any(x in lower for x in ('mgmt', 'management', 'ha', 'dmz')):
                        mgmt.append(entry)
                    elif intf_type in ('physical', 'hard-switch', '') and (lower.startswith('port') or lower.startswith('wan') or lower.startswith('internal') or lower.startswith('npu')):
                        physical.append(entry)
                    elif intf_type in ('physical', 'hard-switch', ''):
                        physical.append(entry)
                    elif intf_type in ('aggregate', 'redundant'):
                        # Aggregate/LAG interfaces go to port-channels
                        tunnels.append(entry)
                    else:
                        # loopback, wifi, etc
                        mgmt.append(entry)
            except Exception as e:
                logger.debug(f"FortiGate get_interfaces error for VDOM {vdom}: {e}")

        # 3) Any CMDB-only interfaces not seen via monitor (unconfigured physical ports)
        for name, cmdb in cmdb_map.items():
            if name in seen:
                continue
            seen.add(name)
            intf_type = cmdb.get('type', '')
            if intf_type not in ('physical', 'hard-switch', ''):
                continue
            speed = self._parse_fg_speed(str(cmdb.get('speed', '')))
            bw = speed * 1_000_000 if speed else 0
            cmdb_status = cmdb.get('status', 'down')
            admin = 'up' if cmdb_status in ('up', 'enable') else 'disabled'
            entry = {
                'name': name,
                'short_name': self._short_name(name),
                'status': 'down', 'admin_status': admin,
                'description': cmdb.get('alias', intf_type),
                'status_color': self._status_color('down', admin),
                'bandwidth': bw, 'speed_label': self._speed_label(bw),
                'mtu': cmdb.get('mtu', ''),
                'mac': cmdb.get('macaddr', ''),
                'ip': '', 'vdom': '', 'type': intf_type, 'media': '',
            }
            physical.append(entry)

        # Sort physical ports numerically
        def _port_sort(e):
            m = re.match(r'port(\d+)', e['name'])
            if m:
                return (0, int(m.group(1)))
            m = re.match(r'wan(\d+)', e['name'])
            if m:
                return (1, int(m.group(1)))
            return (2, 0)
        physical.sort(key=_port_sort)

        return DriverResult(success=True, data={
            'physical_data': physical, 'vlan_data': vlans,
            'port_channel_data': tunnels, 'management_data': mgmt,
        })

    @staticmethod
    def _parse_fg_speed(speed_str):
        """Parse FortiGate speed string to Mbps. Handles: '10000', 'auto', '10000full', '1000full', '10G', etc."""
        if not speed_str:
            return 0
        s = str(speed_str).lower().strip()
        if s in ('auto', 'auto-negotiate', '', 'none'):
            return 0
        # Extract numeric part
        m = re.search(r'(\d+)', s)
        if not m:
            return 0
        val = int(m.group(1))
        # If contains 'g' or value looks like Gbps (1, 10, 25, 40, 100)
        if 'g' in s and val < 1000:
            return val * 1000
        # Values like 10000 = 10000 Mbps
        return val

    def get_health(self):
        try:
            resp = self._get('/api/v2/monitor/system/performance/status')
            if resp.status_code == 200:
                raw = resp.json()
                data = raw.get('results', raw)
                cpu = data.get('cpu', 0)
                if isinstance(cpu, dict):
                    vals = [v for k, v in cpu.items() if isinstance(v, (int, float))]
                    cpu_util = sum(vals) / len(vals) if vals else 0
                elif isinstance(cpu, (int, float)):
                    cpu_util = cpu
                else:
                    cpu_util = 0
                mem = data.get('memory', 0)
                if isinstance(mem, dict):
                    mem_percent = mem.get('used', mem.get('percent_used', 0))
                elif isinstance(mem, (int, float)):
                    mem_percent = mem
                else:
                    mem_percent = 0
                return DriverResult(success=True, data={
                    'cpu_utilization': round(cpu_util, 1),
                    'memory_used': 0, 'memory_total': 0,
                    'memory_percent': round(mem_percent, 1),
                    'uptime': data.get('uptime', 0),
                    'temperature': None,
                })
            return DriverResult(error=f'HTTP {resp.status_code}')
        except Exception as e:
            return DriverResult(error=str(e))

    def get_routes(self):
        """Fetch routes from ALL VDOMs."""
        vdoms = self.get_vdoms()
        routes = []
        for vdom in vdoms:
            try:
                resp = self._get('/api/v2/monitor/router/ipv4', params={'vdom': vdom})
                if resp.status_code == 200:
                    for r in resp.json().get('results', []):
                        prefix = r.get('ip_mask', '')
                        if not prefix:
                            ip = r.get('network', r.get('ip', ''))
                            mask = r.get('mask', r.get('netmask', ''))
                            prefix = f"{ip}/{mask}" if ip else ''
                        routes.append({
                            'vrf': vdom,
                            'prefix': prefix,
                            'protocol': r.get('type', ''),
                            'next_hop': r.get('gateway', 'directly connected'),
                            'interface': r.get('interface', ''),
                            'metric': r.get('metric', ''),
                            'preference': r.get('distance', ''),
                        })
            except Exception as e:
                logger.debug(f"FortiGate get_routes error for VDOM {vdom}: {e}")
        return DriverResult(success=True, data=routes)

    def get_vlans(self):
        try:
            result = self.get_interfaces()
            if result.success:
                vlans = result.data.get('vlan_data', [])
                return DriverResult(success=True, data=[
                    {'id': v['name'].split('.')[-1] if '.' in v['name'] else v['name'],
                     'name': v['name'], 'status': v['status'],
                     'interfaces': v.get('description', '')} for v in vlans
                ])
            return result
        except Exception as e:
            return DriverResult(error=str(e))

    def get_running_config(self):
        try:
            resp = self._get('/api/v2/monitor/system/config/backup',
                           params={'scope': 'global'}, timeout=20)
            if resp.status_code == 200:
                return DriverResult(success=True, data=resp.text)
            return DriverResult(error=f'HTTP {resp.status_code}')
        except Exception as e:
            return DriverResult(error=str(e))

    def get_startup_config(self):
        return self.get_running_config()

    def execute_command(self, command):
        """Allowlisted prefixes: get / show / diagnose / exec ping / exec traceroute."""
        cmd = command.strip()
        allowed_prefixes = ('get', 'show', 'diagnose', 'exec ping', 'exec traceroute')
        if not any(cmd.lower().startswith(p) for p in allowed_prefixes):
            return DriverResult(error="Only 'get', 'show', 'diagnose' commands are allowed.")
        try:
            resp = self._get('/api/v2/monitor/system/cli', params={'commands': cmd}, timeout=15)
            if resp.status_code == 200:
                try:
                    return DriverResult(success=True, data=resp.json().get('results', resp.text))
                except Exception:
                    return DriverResult(success=True, data=resp.text)
            return DriverResult(error=f"HTTP {resp.status_code}")
        except Exception as e:
            return DriverResult(error=str(e))

    def get_arp_table(self):
        vdoms = self.get_vdoms()
        all_entries = []
        for vdom in vdoms:
            try:
                resp = self._get('/api/v2/monitor/network/arp', params={'vdom': vdom})
                if resp.status_code == 200:
                    for e in resp.json().get('results', []):
                        all_entries.append({
                            'ip': e.get('ip', ''),
                            'mac': e.get('mac', ''),
                            'interface': e.get('interface', ''),
                            'age': e.get('age', ''),
                            'vdom': vdom,
                        })
            except Exception:
                pass
        return DriverResult(success=True, data=all_entries)

    def get_mac_table(self):
        return DriverResult(success=True, data=[])

    def get_lldp_neighbors(self):
        """Fetch LLDP neighbors with chassis-id and management-address for topology.
        Uses /api/v2/monitor/network/lldp/neighbors (correct endpoint for FG 7.x).
        Deduplicates across VDOMs since LLDP is at Layer-2."""
        vdoms = self.get_vdoms()
        all_neighbors = []
        seen = set()   # (local_port, remote_chassis) for deduplication
        for vdom in vdoms:
            try:
                resp = self._get('/api/v2/monitor/network/lldp/neighbors', params={'vdom': vdom})
                if resp.status_code != 200:
                    continue
                for n in resp.json().get('results', []):
                    # local_port: prefer 'port_name' (FG 7.4+), fall back to 'port' number
                    local_port = n.get('port_name', '')
                    if not local_port:
                        port_num = n.get('port', '')
                        if port_num != '':
                            local_port = f'port{port_num}'
                    chassis_id = n.get('chassis_id', '')
                    remote_port = n.get('port_id', n.get('port_desc', ''))  # remote interface name
                    dedup_key = (local_port, chassis_id, remote_port)
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    # Skip internal npu vlinks (self-loops)
                    if 'npu' in local_port.lower() and 'npu' in remote_port.lower():
                        continue
                    # Management address from 'addresses' list
                    mgmt_ip = ''
                    addrs = n.get('addresses', [])
                    for a in addrs:
                        if a.get('type') == 'ipv4':
                            mgmt_ip = a.get('address', '')
                            break
                    all_neighbors.append({
                        'local_port': local_port,
                        'remote_device': n.get('system_name', chassis_id),
                        'remote_port': remote_port,
                        'chassis_id': chassis_id,
                        'mgmt_ip': mgmt_ip,
                        'system_description': n.get('system_desc', ''),
                    })
            except Exception:
                pass
        return DriverResult(success=True, data=all_neighbors)

    def get_lldp_neighbors_detail(self):
        return self.get_lldp_neighbors()

    def get_security_policies(self):
        """Fetch firewall policies from ALL VDOMs."""
        vdoms = self.get_vdoms()
        all_policies = []
        for vdom in vdoms:
            try:
                resp = self._get('/api/v2/cmdb/firewall/policy', params={'vdom': vdom})
                if resp.status_code == 200:
                    for p in resp.json().get('results', []):
                        def _names(lst):
                            if isinstance(lst, list):
                                return ', '.join(i.get('name', str(i)) for i in lst)
                            return str(lst)
                        vdom_label = f" [{vdom}]" if len(vdoms) > 1 else ""
                        all_policies.append({
                            'id': p.get('policyid', ''),
                            'name': f"{p.get('name', '')}{vdom_label}",
                            'srcintf': _names(p.get('srcintf', [])),
                            'dstintf': _names(p.get('dstintf', [])),
                            'srcaddr': _names(p.get('srcaddr', [])),
                            'dstaddr': _names(p.get('dstaddr', [])),
                            'service': _names(p.get('service', [])),
                            'action': p.get('action', ''),
                            'status': 'enabled' if p.get('status') == 'enable' else 'disabled',
                            'log': p.get('logtraffic', ''),
                            'vdom': vdom,
                        })
            except Exception as e:
                logger.debug(f"FortiGate policies error for VDOM {vdom}: {e}")
        return DriverResult(success=True, data=all_policies)

    def get_vpn_tunnels(self):
        """Fetch VPN tunnels from ALL VDOMs."""
        vdoms = self.get_vdoms()
        all_tunnels = []
        for vdom in vdoms:
            try:
                resp = self._get('/api/v2/monitor/vpn/ipsec', params={'vdom': vdom})
                if resp.status_code == 200:
                    for t in resp.json().get('results', []):
                        proxyid = t.get('proxyid', [])
                        tunnel_status = 'up' if any(
                            p.get('status', '') == 'up' for p in proxyid
                        ) else t.get('connection', 'down')
                        all_tunnels.append({
                            'name': t.get('name', ''),
                            'type': t.get('type', 'IPSec'),
                            'remote_gw': t.get('rgwy', t.get('remote-gateway', '')),
                            'status': 'up' if tunnel_status == 'up' else 'down',
                            'incoming_bytes': t.get('incoming_bytes', t.get('tun_id_bytes_in', 0)),
                            'outgoing_bytes': t.get('outgoing_bytes', t.get('tun_id_bytes_out', 0)),
                            'vdom': vdom,
                        })
            except Exception:
                pass
        return DriverResult(success=True, data=all_tunnels)

    def get_ha_status(self):
        try:
            resp = self._get('/api/v2/monitor/system/ha-peer')
            if resp.status_code == 200:
                return DriverResult(success=True, data=resp.json().get('results', []))
            return DriverResult(error=f'HTTP {resp.status_code}')
        except Exception as e:
            return DriverResult(error=str(e))

    # ---- New driver methods for enhanced features ----

    def get_bgp_summary(self):
        vdoms = self.get_vdoms()
        peers = []
        for vdom in vdoms:
            try:
                resp = self._get('/api/v2/monitor/router/bgp/neighbors', params={'vdom': vdom})
                if resp.status_code == 200:
                    for n in resp.json().get('results', []):
                        peers.append({
                            'neighbor': n.get('neighbor-ip', n.get('ip', '')),
                            'asn': n.get('remote-as', ''),
                            'state': n.get('state', 'unknown'),
                            'prefixes_received': n.get('prefixes-received', n.get('accepted-prefix-count', 0)),
                            'uptime': n.get('uptime', ''),
                            'vrf': vdom,
                        })
            except Exception:
                pass
        return DriverResult(success=True, data=peers)

    def get_ospf_neighbors(self):
        vdoms = self.get_vdoms()
        neighbors = []
        for vdom in vdoms:
            try:
                resp = self._get('/api/v2/monitor/router/ospf/neighbors', params={'vdom': vdom})
                if resp.status_code == 200:
                    for n in resp.json().get('results', []):
                        neighbors.append({
                            'neighbor_id': n.get('neighbor-ip', n.get('router-id', '')),
                            'address': n.get('neighbor-ip', ''),
                            'state': n.get('state', ''),
                            'interface': n.get('interface', ''),
                            'area': n.get('area', ''),
                            'priority': n.get('priority', ''),
                        })
            except Exception:
                pass
        return DriverResult(success=True, data=neighbors)

    def get_environment(self):
        try:
            resp = self._get('/api/v2/monitor/system/sensor-info')
            if resp.status_code == 200:
                sensors = resp.json().get('results', [])
                return DriverResult(success=True, data={
                    'sensors': [{'name': s.get('name', ''), 'value': s.get('value', ''),
                                'type': s.get('type', ''), 'status': s.get('alarm', 'ok')
                                } for s in sensors],
                    'fans': [], 'power_supplies': [],
                })
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(success=True, data={'sensors': [], 'fans': [], 'power_supplies': []})

    def get_interface_counters(self):
        """Fetch interface traffic counters."""
        vdoms = self.get_vdoms()
        counters = []
        for vdom in vdoms[:1]:  # counters from first vdom is enough
            try:
                resp = self._get('/api/v2/monitor/system/interface', params={'vdom': vdom})
                if resp.status_code == 200:
                    for intf in resp.json().get('results', []):
                        counters.append({
                            'name': intf.get('name', ''),
                            'bytes_in': intf.get('rx_bytes', 0),
                            'bytes_out': intf.get('tx_bytes', 0),
                            'packets_in': intf.get('rx_packets', 0),
                            'packets_out': intf.get('tx_packets', 0),
                            'errors_in': intf.get('rx_errors', 0),
                            'errors_out': intf.get('tx_errors', 0),
                        })
            except Exception:
                pass
        return DriverResult(success=True, data=counters)

    def get_port_channel_members(self):
        return DriverResult(success=True, data={})

    def get_dom_info(self):
        return DriverResult(success=True, data=[])

    # ---- Configuration Push ----

    def send_config(self, commands, commit=True):
        """Push config via FortiGate REST API. Each command is a dict with path + data,
        or a string CLI command (sent via /api/v2/monitor/system/cli/execute)."""
        outputs = []
        errors = []
        for cmd in commands:
            try:
                if isinstance(cmd, dict):
                    # REST API: {'method': 'PUT', 'path': '/api/v2/cmdb/...', 'data': {...}, 'params': {...}}
                    method = cmd.get('method', 'PUT').upper()
                    path = cmd.get('path', '')
                    data = cmd.get('data', {})
                    params = cmd.get('params', {})
                    if method == 'PUT':
                        resp = self._put(path, data=data, params=params)
                    else:
                        resp = self._get(path, params=params)
                    outputs.append({'path': path, 'status': resp.status_code,
                                   'result': resp.json().get('status', '') if resp.text else ''})
                else:
                    # CLI command - not directly supported via REST API in most FG versions
                    outputs.append({'command': cmd, 'status': 'skipped',
                                   'result': 'CLI commands not directly supported via REST'})
            except Exception as e:
                errors.append({'command': str(cmd)[:100], 'error': str(e)})

        return DriverResult(
            success=len(errors) == 0,
            data={'outputs': outputs, 'errors': errors, 'commands_sent': len(commands)},
            error='; '.join(e['error'] for e in errors) if errors else '',
        )

    def _put(self, path, data=None, params=None, timeout=10):
        """PUT request to FortiGate API."""
        session = _get_session(self.ip)
        self._ensure_auth(session)
        url = f"{self._base_url()}{path}"
        all_params = dict(params or {})
        if self.api_key:
            all_params['access_token'] = self.api_key
        resp = session.put(url, params=all_params, json=data, timeout=timeout)
        return resp

    def enable_lldp(self):
        """Enable LLDP TX/RX on all physical interfaces across all VDOMs."""
        vdoms = self.get_vdoms()
        enabled_count = 0
        details = []

        # Get all interfaces from CMDB
        try:
            resp = self._get('/api/v2/cmdb/system/interface')
            if resp.status_code != 200:
                return DriverResult(error=f'Cannot list interfaces: HTTP {resp.status_code}')
            interfaces = resp.json().get('results', [])
        except Exception as e:
            return DriverResult(error=f'Cannot list interfaces: {e}')

        for intf in interfaces:
            name = intf.get('name', '')
            intf_type = intf.get('type', '')
            if intf_type not in ('physical', 'hard-switch'):
                continue

            # Determine VDOM
            intf_vdom = intf.get('vdom', '')
            if isinstance(intf_vdom, list) and intf_vdom:
                intf_vdom = intf_vdom[0].get('name', 'root') if isinstance(intf_vdom[0], dict) else str(intf_vdom[0])
            if not intf_vdom:
                intf_vdom = 'root'

            try:
                resp = self._put(
                    f'/api/v2/cmdb/system/interface/{name}',
                    data={'lldp-reception': 'enable', 'lldp-transmission': 'enable'},
                    params={'vdom': intf_vdom},
                )
                if resp.status_code == 200 and resp.json().get('status') == 'success':
                    enabled_count += 1
                else:
                    details.append(f'{name}: HTTP {resp.status_code}')
            except Exception as e:
                details.append(f'{name}: {e}')

        details.insert(0, f'LLDP enabled on {enabled_count}/{len(interfaces)} physical interfaces')
        return DriverResult(success=enabled_count > 0, data={
            'enabled_count': enabled_count,
            'details': details,
        })
