"""
SONiC driver - communicates via REST API (HTTPS) and RESTCONF.
Uses interface aliases (from platform config) to show physical front-panel port names
instead of internal lane-based names like Ethernet0, Ethernet4, etc.
Enhanced: BGP, OSPF, environment, counters, port-channel members.

Transports, in fallback order per call:

1. RESTCONF ``GET /restconf/data/...`` (OpenConfig / sonic-* YANG models).
2. CLI-over-HTTP ``POST /api/v1/cli`` with ``{"command": "..."}``.
3. SSH (Paramiko, connect timeout 10 s) running the same CLI command.

``_try_request`` tries https then http on ``api_port`` (default 443), two rounds,
with TLS verification disabled. Only ``self.ip`` (the first connect target) is
used; this driver does not iterate dual-stack targets.
"""
import re
import logging
import requests
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning

from .base import BaseDriver, DriverResult

logger = logging.getLogger(__name__)
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

_sessions = {}
_alias_cache = {}


def _get_session(ip):
    if ip not in _sessions:
        s = requests.Session()
        s.verify = False
        _sessions[ip] = s
    return _sessions[ip]


def clear_cache(ip):
    """Drop the cached HTTP session and interface-alias map for *ip*."""
    _sessions.pop(ip, None)
    _alias_cache.pop(ip, None)


class SonicDriver(BaseDriver):
    """SONiC switch driver (RESTCONF → ``/api/v1/cli`` → SSH fallbacks).

    Also used as a delegate by :class:`~connect.drivers.arista.AristaDriver` when a
    dual-OS box is currently running SONiC.
    """

    VENDOR_NAME = 'sonic'

    def _base_url(self, proto=None):
        p = proto or ('https' if self.transport in ('auto', 'https') else 'http')
        port = self.api_port or 443
        return f"{p}://{self.ip}:{port}"

    def _auth(self):
        return HTTPBasicAuth(self.username, self.password)

    def _get(self, path, proto=None, timeout=10):
        session = _get_session(self.ip)
        url = f"{self._base_url(proto)}{path}"
        return session.get(url, auth=self._auth(), timeout=timeout)

    def _post(self, path, data=None, proto=None, timeout=10):
        session = _get_session(self.ip)
        url = f"{self._base_url(proto)}{path}"
        return session.post(url, auth=self._auth(), json=data, timeout=timeout)

    def _try_request(self, method, path, data=None, timeout=10, retries=2):
        """Try request with retries for transient connection failures (common on SONiC RESTCONF)."""
        last_exc = None
        for attempt in range(retries):
            for proto in ('https', 'http'):
                try:
                    if method == 'GET':
                        resp = self._get(path, proto=proto, timeout=timeout)
                    else:
                        resp = self._post(path, data=data, proto=proto, timeout=timeout)
                    return resp, proto
                except Exception as e:
                    last_exc = e
                    continue
        if last_exc:
            logger.debug(f"SONiC {self.ip} {method} {path}: {last_exc}")
        return None, None

    def _cli_output(self, command, timeout=10):
        """Execute a CLI command and return output text."""
        resp, _ = self._try_request('POST', '/api/v1/cli', data={'command': command}, timeout=timeout)
        if resp and resp.status_code == 200:
            try:
                return resp.json().get('output', resp.text)
            except Exception:
                return resp.text
        return ''

    def _ssh_run(self, command, timeout=15):
        """Run a command via SSH. Returns output or empty string.
        Tries device.username/password; when the stored password equals a common vendor
        default it also retries once with the other common SONiC default.
        """
        import paramiko
        cred_sets = [(self.username, self.password)]
        if self.password == 'admin':
            cred_sets.append((self.username, 'password'))
        for u, p in cred_sets:
            client = None
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    self.ip, username=u, password=p,
                    timeout=10, look_for_keys=False, allow_agent=False,
                )
                _, stdout, _ = client.exec_command(command, timeout=timeout)
                out = stdout.read().decode('utf-8', errors='replace')
                client.close()
                return out
            except paramiko.ssh_exception.AuthenticationException:
                logger.debug('Sonic SSH %s auth failed (user=%s)', self.ip, u)
                if client:
                    try: client.close()
                    except Exception: pass
                continue
            except Exception as e:
                logger.debug('Sonic SSH %s failed: %s', self.ip, e)
                if client:
                    try: client.close()
                    except Exception: pass
                return ''
        return ''

    def _get_interfaces_ssh(self):
        """Get interfaces via SSH 'show interfaces status' — reliable fallback for SONiC boxes."""
        raw = self._ssh_run('show interfaces status', timeout=15)
        if not raw:
            return None
        physical = []
        lines = raw.strip().splitlines()
        # Find header line
        header_idx = next((i for i, l in enumerate(lines) if 'Interface' in l and 'Oper' in l), 1)
        for line in lines[header_idx + 2:]:
            if not line.strip() or line.strip().startswith('-'):
                continue
            parts = line.split()
            if len(parts) < 7:
                continue
            name = parts[0]
            if not name.startswith('Ethernet'):
                continue
            # Columns: Interface Lanes Speed MTU FEC Alias Vlan Oper Admin Type ...
            alias = parts[5] if len(parts) > 5 else name
            oper = parts[7] if len(parts) > 7 else 'down'
            admin = parts[8] if len(parts) > 8 else 'up'
            speed = parts[2] if len(parts) > 2 else ''
            oper_norm = 'up' if 'up' in oper.lower() else 'down'
            admin_norm = 'up' if 'up' in admin.lower() else 'disabled'
            bw = self._parse_speed(speed)
            physical.append({
                'name': name,
                'display_name': alias or name,
                'short_name': alias or name,
                'alias': alias,
                'status': oper_norm,
                'admin_status': admin_norm,
                'description': '',
                'status_color': self._status_color(oper_norm, admin_norm),
                'bandwidth': bw,
                'speed_label': speed or self._speed_label(bw),
                'mtu': parts[3] if len(parts) > 3 else '',
                'mac': '',
            })
        physical.sort(key=lambda i: self._intf_sort(i['name']))
        if not physical:
            return None
        return DriverResult(success=True, data={
            'physical_data': physical, 'vlan_data': [],
            'port_channel_data': [], 'management_data': [],
        })

    def probe(self):
        """RESTCONF system state, then ``show version`` via ``/api/v1/cli`` (no SSH probe)."""
        resp, _ = self._try_request('GET', '/restconf/data/openconfig-system:system/state', timeout=12)
        if resp is not None:
            if resp.status_code == 200:
                return 'ok'
            elif resp.status_code == 401:
                return 'auth_failed'
        resp, _ = self._try_request('POST', '/api/v1/cli', data={'command': 'show version'}, timeout=12)
        if resp is not None:
            if resp.status_code == 200:
                return 'ok'
            elif resp.status_code == 401:
                return 'auth_failed'
        return 'unreachable'

    def _load_aliases(self):
        if self.ip in _alias_cache:
            return _alias_cache[self.ip]
        aliases = {}
        try:
            resp, _ = self._try_request('GET',
                '/restconf/data/sonic-port:sonic-port/PORT/PORT_LIST', timeout=12)
            if resp and resp.status_code == 200:
                data = resp.json()
                port_list = data.get('sonic-port:PORT_LIST', data.get('PORT_LIST', []))
                for port in port_list:
                    name = port.get('name', port.get('ifname', ''))
                    alias = port.get('alias', '')
                    if name and alias:
                        aliases[name] = alias
        except Exception:
            pass
        if not aliases:
            try:
                output = self._cli_output('show interfaces alias')
                if isinstance(output, str):
                    for line in output.strip().splitlines():
                        parts = line.split()
                        if len(parts) >= 2 and parts[0].startswith('Ethernet'):
                            aliases[parts[0]] = parts[1]
            except Exception:
                pass
        _alias_cache[self.ip] = aliases
        return aliases

    def get_system_info(self):
        try:
            resp, _ = self._try_request('GET', '/restconf/data/openconfig-system:system/state', timeout=15)
            if resp and resp.status_code == 200:
                data = resp.json()
                state = data.get('openconfig-system:state', data.get('state', {}))
                return DriverResult(success=True, data={
                    'hostname': state.get('hostname', self.ip),
                    'version': state.get('software-version', ''),
                    'serial_number': state.get('serial-number', ''),
                    'model_name': state.get('hardware-model', 'SONiC Switch'),
                    'mac_address': state.get('mac-address', ''),
                    'uptime': self._parse_uptime(state.get('boot-time', '')),
                    'vendor_detail': f"SONiC {state.get('software-version', '')}",
                })
        except Exception:
            pass
        try:
            output = self._cli_output('show version')
            hostname = self.ip
            version = 'SONiC'
            model = 'SONiC Switch'
            if isinstance(output, str):
                for line in output.splitlines():
                    if 'SONiC Software Version' in line:
                        version = line.split(':')[-1].strip()
                    elif 'Platform' in line:
                        model = line.split(':')[-1].strip()
                    elif 'HwSKU' in line:
                        model = line.split(':')[-1].strip()
            return DriverResult(success=True, data={
                'hostname': hostname, 'version': version,
                'serial_number': '', 'model_name': model,
                'mac_address': '', 'uptime': None,
                'vendor_detail': version,
            })
        except Exception as e:
            return DriverResult(error=str(e))

    def get_interfaces(self):
        """OpenConfig interfaces → CLI ``show interfaces status`` → SSH, alias-labelled."""
        aliases = self._load_aliases()
        # RESTCONF interfaces returns large payload (~128KB); use longer timeout and retries
        try:
            resp, _ = self._try_request('GET', '/restconf/data/openconfig-interfaces:interfaces', timeout=25)
            if resp and resp.status_code == 200:
                data = resp.json()
                intfs = data.get('openconfig-interfaces:interfaces', {}).get('interface', [])
                physical, vlans, portchannels, mgmt = [], [], [], []
                for intf in intfs:
                    name = intf.get('name', '')
                    config = intf.get('config', {})
                    state = intf.get('state', {})
                    link = (state.get('oper-status', '') or 'DOWN').lower()
                    admin = (state.get('admin-status', '') or 'DOWN').lower()
                    link_norm = 'up' if link == 'up' else 'down'
                    admin_norm = 'up' if admin == 'up' else 'disabled'
                    desc = config.get('description', state.get('description', ''))
                    mtu = config.get('mtu', state.get('mtu', ''))
                    alias = aliases.get(name, '')
                    display_name = alias if alias else name
                    short = self._short_name(display_name) if not alias else alias
                    eth = intf.get('openconfig-if-ethernet:ethernet', {})
                    eth_state = eth.get('state', {})
                    port_speed = eth_state.get('port-speed', eth_state.get('negotiated-port-speed', ''))
                    bw = self._parse_speed(port_speed)
                    entry = {
                        'name': name, 'display_name': display_name,
                        'short_name': short, 'alias': alias,
                        'status': link_norm, 'admin_status': admin_norm,
                        'description': desc,
                        'status_color': self._status_color(link_norm, admin_norm),
                        'bandwidth': bw, 'speed_label': self._speed_label(bw) if bw else port_speed,
                        'mtu': mtu, 'mac': '',
                    }
                    lower = name.lower()
                    if 'vlan' in lower:
                        vlans.append(entry)
                    elif 'portchannel' in lower or 'bond' in lower:
                        portchannels.append(entry)
                    elif lower in ('eth0', 'management0') or 'management' in lower:
                        mgmt.append(entry)
                    elif lower.startswith('ethernet') or lower.startswith('eth'):
                        physical.append(entry)
                physical.sort(key=lambda i: self._intf_sort(i['name']))
                return DriverResult(success=True, data={
                    'physical_data': physical, 'vlan_data': vlans,
                    'port_channel_data': portchannels, 'management_data': mgmt,
                })
        except Exception as e:
            logger.debug(f"SONiC RESTCONF interfaces error: {e}")

        try:
            resp, _ = self._try_request('POST', '/api/v1/cli',
                data={'command': 'show interfaces status'}, timeout=15)
            if resp and resp.status_code == 200:
                physical, vlans, portchannels, mgmt = [], [], [], []
                try:
                    output = resp.json().get('output', resp.text)
                except Exception:
                    output = resp.text
                if isinstance(output, str):
                    for line in output.strip().splitlines()[2:]:
                        parts = line.split()
                        if len(parts) >= 4:
                            name = parts[0]
                            alias = aliases.get(name, name)
                            status = parts[-2] if len(parts) >= 5 else 'down'
                            speed = parts[-1] if len(parts) >= 5 else ''
                            link = 'up' if 'up' in status.lower() else 'down'
                            bw = self._parse_speed(speed)
                            entry = {
                                'name': name, 'display_name': alias,
                                'short_name': alias, 'alias': alias,
                                'status': link, 'admin_status': link,
                                'description': '', 'status_color': self._status_color(link, link),
                                'bandwidth': bw, 'speed_label': self._speed_label(bw) or speed,
                                'mtu': '', 'mac': '',
                            }
                            lower = name.lower()
                            if 'vlan' in lower:
                                vlans.append(entry)
                            elif 'portchannel' in lower:
                                portchannels.append(entry)
                            elif lower == 'eth0':
                                mgmt.append(entry)
                            else:
                                physical.append(entry)
                physical.sort(key=lambda i: self._intf_sort(i['name']))
                return DriverResult(success=True, data={
                    'physical_data': physical, 'vlan_data': vlans,
                    'port_channel_data': portchannels, 'management_data': mgmt,
                })
        except Exception as e:
            return DriverResult(error=str(e))
        # Final fallback: SSH-based interface status (works even when REST/CLI API is down)
        ssh_result = self._get_interfaces_ssh()
        if ssh_result:
            return ssh_result
        return DriverResult(error='Cannot retrieve interfaces')

    def get_health(self):
        try:
            resp, _ = self._try_request('POST', '/api/v1/cli', data={'command': 'show system-memory'})
            if resp and resp.status_code == 200:
                mem_used, mem_total = 0, 0
                try:
                    output = resp.json().get('output', resp.text)
                except Exception:
                    output = resp.text
                if isinstance(output, str):
                    for line in output.splitlines():
                        if 'Mem:' in line:
                            parts = line.split()
                            if len(parts) >= 3:
                                try:
                                    mem_total = int(parts[1])
                                    mem_used = int(parts[2])
                                except (ValueError, IndexError):
                                    pass
                mem_pct = round(mem_used / mem_total * 100, 1) if mem_total else 0
                return DriverResult(success=True, data={
                    'cpu_utilization': 0, 'memory_used': mem_used,
                    'memory_total': mem_total, 'memory_percent': mem_pct,
                    'uptime': 0, 'temperature': None,
                })
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve health data')

    def get_routes(self):
        try:
            resp, _ = self._try_request('POST', '/api/v1/cli',
                data={'command': 'show ip route json'})
            if resp and resp.status_code == 200:
                routes = []
                try:
                    data = resp.json()
                except Exception:
                    return DriverResult(success=True, data=[])
                if isinstance(data, dict):
                    for prefix, info_list in data.items():
                        if isinstance(info_list, list):
                            for info in info_list:
                                for nh in info.get('nexthops', [{}]):
                                    routes.append({
                                        'vrf': 'default', 'prefix': prefix,
                                        'protocol': info.get('protocol', ''),
                                        'next_hop': nh.get('ip', 'directly connected'),
                                        'interface': nh.get('interfaceName', ''),
                                        'metric': info.get('metric', ''),
                                        'preference': info.get('distance', ''),
                                    })
                return DriverResult(success=True, data=routes)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve routes')

    def get_vlans(self):
        try:
            resp, _ = self._try_request('POST', '/api/v1/cli',
                data={'command': 'show vlan brief'})
            if resp and resp.status_code == 200:
                vlans = []
                try:
                    output = resp.json().get('output', resp.text)
                except Exception:
                    output = resp.text
                if isinstance(output, str):
                    for line in output.strip().splitlines()[2:]:
                        parts = line.split()
                        if len(parts) >= 2:
                            try:
                                vid = int(parts[0])
                                vlans.append({
                                    'id': str(vid), 'name': parts[1] if len(parts) > 1 else '',
                                    'status': parts[2] if len(parts) > 2 else 'active',
                                    'interfaces': ' '.join(parts[3:]) if len(parts) > 3 else '',
                                })
                            except ValueError:
                                pass
                return DriverResult(success=True, data=vlans)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve VLANs')

    def get_running_config(self):
        try:
            resp, _ = self._try_request('POST', '/api/v1/cli',
                data={'command': 'show running-configuration'})
            if resp and resp.status_code == 200:
                try:
                    out = resp.json().get('output', resp.text)
                except Exception:
                    out = resp.text
                return DriverResult(success=True, data=out)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve config')

    def get_startup_config(self):
        return self.get_running_config()

    def execute_command(self, command):
        """Run one ``show ...`` command through ``/api/v1/cli``; other prefixes rejected."""
        cmd = command.strip()
        if not cmd.lower().startswith('show'):
            return DriverResult(error="Only 'show' commands are allowed.")
        try:
            resp, _ = self._try_request('POST', '/api/v1/cli', data={'command': cmd})
            if resp and resp.status_code == 200:
                try:
                    out = resp.json().get('output', resp.text)
                except Exception:
                    out = resp.text
                return DriverResult(success=True, data=out)
            return DriverResult(error=f"HTTP {resp.status_code}" if resp else "No response")
        except Exception as e:
            return DriverResult(error=str(e))

    def _parse_lldp_neighbors_verbose(self, output):
        """Parse 'show lldp neighbors' verbose output into neighbor dicts."""
        neighbors = []
        blocks = re.split(r'\n(?=Interface:\s+)', output)
        for block in blocks:
            if not block.strip():
                continue
            lines = block.splitlines()
            local_port = remote_device = remote_port = chassis_id = mgmt_ip = ''
            port_descr = ''
            for line in lines:
                if 'Interface:' in line:
                    m = re.search(r'Interface:\s+(\S+)', line)
                    if m:
                        local_port = m.group(1).rstrip(',')
                elif 'ChassisID:' in line:
                    val = line.split('ChassisID:', 1)[-1].strip()
                    if val.lower().startswith('mac '):
                        chassis_id = val[4:].strip()
                    else:
                        chassis_id = val
                elif 'SysName:' in line:
                    remote_device = line.split('SysName:', 1)[-1].strip()
                elif 'MgmtIP:' in line and not mgmt_ip:
                    mgmt_ip = line.split('MgmtIP:', 1)[-1].strip()
                elif 'PortID:' in line:
                    val = line.split('PortID:', 1)[-1].strip()
                    if val.lower().startswith('mac '):
                        val = val[4:]
                    if val.lower().startswith('ifname '):
                        val = val[7:]
                    remote_port = val
                elif 'PortDescr:' in line:
                    port_descr = line.split('PortDescr:', 1)[-1].strip()
            if local_port:
                # Prefer PortDescr (e.g. enp61s0np0) over PortID when PortDescr looks like interface name
                rport = (port_descr if port_descr and not port_descr[0].isdigit() else remote_port) or port_descr or remote_port
                neighbors.append({
                    'local_port': local_port,
                    'remote_device': remote_device or rport or 'unknown',
                    'remote_port': rport or '',
                    'chassis_id': chassis_id,
                    'mgmt_ip': mgmt_ip,
                })
        return neighbors

    @staticmethod
    def _neighbors_from_show_lldp_table_text(output: str) -> list:
        """Parse ``show lldp table`` / ``show lldp table -n`` style text; skip headers and rule lines."""
        if not output or ('LocalPort' not in output and 'Local Port' not in output):
            return []
        lines = output.strip().splitlines()
        header_idx = next((i for i, l in enumerate(lines) if 'LocalPort' in l or 'Local Port' in l), 0)
        out = []
        for line in lines[header_idx + 1:]:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('---') or set(stripped) <= {'-', '|', ' '}:
                continue
            low = stripped.lower()
            if 'total entries displayed' in low or low.startswith('total '):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            loc, rem, rport = parts[0], parts[1], parts[2]
            if set(loc) <= {'-'} or set(rem) <= {'-'}:
                continue
            if rem in ('--------------', 'Capability', 'entries', 'displayed:'):
                continue
            if loc in ('Capability', 'LocalPort', 'Local', 'Port'):
                continue
            out.append({
                'local_port': loc,
                'remote_device': rem,
                'remote_port': rport,
                'chassis_id': '',
                'mgmt_ip': '',
            })
        return out

    @staticmethod
    def _lldp_neighbors_from_sonic_yang(data: dict) -> list:
        """Parse SONiC YANG LLDP RESTCONF (LLDP_ENTRY_TABLE_LIST or keyed rows).

        Native ``show lldp table`` works while RESTCONF often uses ``lldp_rem_*`` keys
        and a list keyed by ``ifname``, not a flat dict of Ethernet -> attrs.
        """
        neighbors = []
        if not isinstance(data, dict):
            return neighbors

        def row_to_nbr(local_port: str, row: dict):
            if not local_port or not isinstance(row, dict):
                return None
            local_port = local_port.split('|')[0].strip()
            rem = (
                row.get('lldp_rem_sys_name')
                or row.get('remote_system_name')
                or row.get('remote_hostname')
                or row.get('system_name')
                or row.get('lldp_rem_system_name')
                or ''
            )
            if isinstance(rem, str):
                rem = rem.strip()
            rport = (
                row.get('lldp_rem_port_id')
                or row.get('lldp_rem_port_desc')
                or row.get('remote_port')
                or row.get('port_id')
                or row.get('port-id')
                or ''
            )
            if isinstance(rport, str):
                rport = rport.strip()
            cid = row.get('lldp_rem_chassis_id', row.get('chassis_id', '')) or ''
            if isinstance(cid, str):
                cid = cid.strip()
            mgmt = row.get('lldp_rem_man_addr', row.get('management_ip', row.get('mgmt_ip', ''))) or ''
            if isinstance(mgmt, str):
                mgmt = mgmt.strip()
            if not rem and not rport:
                return None
            return {
                'local_port': local_port,
                'remote_device': rem,
                'remote_port': rport,
                'chassis_id': cid,
                'mgmt_ip': mgmt,
            }

        sl = data.get('sonic-lldp:sonic-lldp') or data.get('sonic-lldp')
        candidates = [data]
        if isinstance(sl, dict):
            candidates.append(sl)

        for blob in candidates:
            table = (
                blob.get('LLDP_ENTRY_TABLE')
                or blob.get('sonic-lldp:LLDP_ENTRY_TABLE')
            )
            if not isinstance(table, dict):
                continue
            entry_list = table.get('LLDP_ENTRY_TABLE_LIST')
            if isinstance(entry_list, list):
                for row in entry_list:
                    if not isinstance(row, dict):
                        continue
                    ifname = row.get('ifname') or row.get('name') or row.get('port') or ''
                    n = row_to_nbr(str(ifname), row)
                    if n:
                        neighbors.append(n)
            for k, v in table.items():
                if k == 'LLDP_ENTRY_TABLE_LIST' or not isinstance(v, dict):
                    continue
                n = row_to_nbr(str(k), v)
                if n:
                    neighbors.append(n)

            top_list = blob.get('LLDP_ENTRY_TABLE_LIST')
            if isinstance(top_list, list):
                for row in top_list:
                    if isinstance(row, dict):
                        ifname = row.get('ifname') or row.get('name', '') or ''
                        n = row_to_nbr(str(ifname), row)
                        if n:
                            neighbors.append(n)

        return neighbors

    def get_lldp_neighbors(self):
        """LLDP via RESTCONF (4 paths) → CLI ``show lldp table [json]`` → SSH text parse."""
        # Try OpenConfig REST first (multiple possible paths for different SONiC versions)
        try:
            for lldp_path in [
                '/restconf/data/openconfig-lldp:lldp/interfaces',
                '/restconf/data/sonic-lldp:sonic-lldp/LLDP_ENTRY_TABLE',
                '/restconf/data/sonic-lldp:sonic-lldp',
                '/restconf/data/openconfig-lldp:lldp',
            ]:
                resp, _ = self._try_request('GET', lldp_path, timeout=10)
                if not (resp and resp.status_code == 200 and resp.text):
                    continue
                data = resp.json()
                neighbors = []
                intfs = data.get('openconfig-lldp:interfaces', {}).get('interface', [])
                if not intfs:
                    intfs = data.get('openconfig-lldp:lldp', {}).get('interfaces', {}).get('interface', [])
                yang_n = self._lldp_neighbors_from_sonic_yang(data)
                if yang_n:
                    neighbors.extend(yang_n)
                if not neighbors and not intfs and 'LLDP_ENTRY_TABLE' in str(data):
                    legacy = data.get('sonic-lldp:LLDP_ENTRY_TABLE', data.get('LLDP_ENTRY_TABLE', {}))
                    if isinstance(legacy, dict):
                        for k, v in legacy.items():
                            if isinstance(v, dict):
                                neighbors.append({
                                    'local_port': str(k).split('|')[0],
                                    'remote_device': v.get('remote_hostname', '') or v.get('lldp_rem_sys_name', ''),
                                    'remote_port': v.get('remote_port', '') or v.get('lldp_rem_port_id', ''),
                                    'chassis_id': '', 'mgmt_ip': '',
                                })
                for intf in intfs:
                    for n in intf.get('neighbors', {}).get('neighbor', []):
                        state = n.get('state', {})
                        neighbors.append({
                            'local_port': intf.get('name', ''),
                            'remote_device': (
                                state.get('system-name')
                                or state.get('system_name')
                                or ''
                            ),
                            'remote_port': (
                                state.get('port-id')
                                or state.get('port_id')
                                or ''
                            ),
                            'chassis_id': (
                                state.get('chassis-id')
                                or state.get('chassis_id')
                                or ''
                            ),
                            'mgmt_ip': (
                                state.get('management-address')
                                or state.get('management_address')
                                or ''
                            ),
                        })
                if neighbors:
                    return DriverResult(success=True, data=neighbors)
        except Exception as e:
            logger.debug('Sonic LLDP REST failed: %s', e)

        # Fallback: CLI "show lldp table" or "show lldp neighbors"
        try:
            output = self._cli_output('show lldp table json')
            if output:
                import json
                data = json.loads(output)
                neighbors = []
                # SONiC format: {"lldp": {"interface": {"Ethernet0": {"neighbor": {...}}}}}
                lldp = data.get('lldp', data)
                intfs = lldp.get('interface', lldp.get('interfaces', {}))
                if isinstance(intfs, dict):
                    for local_port, intf_data in intfs.items():
                        nbr = intf_data.get('neighbor', {}) if isinstance(intf_data, dict) else {}
                        if nbr:
                            neighbors.append({
                                'local_port': local_port,
                                'remote_device': nbr.get('chassis', {}).get('name', nbr.get('system-name', '')),
                                'remote_port': nbr.get('port', {}).get('id', nbr.get('port-id', '')),
                                'chassis_id': nbr.get('chassis', {}).get('id', nbr.get('chassis-id', '')),
                                'mgmt_ip': nbr.get('chassis', {}).get('mgmt-ip', nbr.get('management-address', '')),
                            })
                if neighbors:
                    return DriverResult(success=True, data=neighbors)
        except Exception:
            pass

        try:
            output = self._cli_output('show lldp table')
            neighbors = self._neighbors_from_show_lldp_table_text(output or '')
            if neighbors:
                return DriverResult(success=True, data=neighbors)
        except Exception:
            pass

        # Fallback: SSH when REST/CLI API unavailable (e.g. /api/v1/cli returns 404)
        try:
            output = self._ssh_run('show lldp table')
            neighbors = self._neighbors_from_show_lldp_table_text(output or '')
            if neighbors:
                return DriverResult(success=True, data=neighbors)
        except Exception:
            pass

        try:
            output = self._ssh_run('show lldp neighbors')
            if output and 'Interface:' in output:
                neighbors = self._parse_lldp_neighbors_verbose(output)
                if neighbors:
                    return DriverResult(success=True, data=neighbors)
        except Exception:
            pass

        return DriverResult(error='Cannot retrieve LLDP neighbors')

    def get_lldp_neighbors_detail(self):
        return self.get_lldp_neighbors()

    # ---- Enhanced driver methods ----

    def get_bgp_summary(self):
        try:
            output = self._cli_output('show ip bgp summary json')
            if output and isinstance(output, str):
                import json
                try:
                    data = json.loads(output)
                except Exception:
                    return DriverResult(success=True, data=[])
                peers = []
                for peer_addr, peer_info in data.get('ipv4Unicast', {}).get('peers', {}).items():
                    peers.append({
                        'neighbor': peer_addr,
                        'asn': peer_info.get('remoteAs', ''),
                        'state': peer_info.get('state', 'unknown'),
                        'prefixes_received': peer_info.get('pfxRcd', 0),
                        'uptime': peer_info.get('peerUptime', ''),
                        'vrf': 'default',
                    })
                return DriverResult(success=True, data=peers)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(success=True, data=[])

    def get_ospf_neighbors(self):
        try:
            output = self._cli_output('show ip ospf neighbor json')
            if output and isinstance(output, str):
                import json
                try:
                    data = json.loads(output)
                except Exception:
                    return DriverResult(success=True, data=[])
                neighbors = []
                for nbr in data.get('neighbors', {}).values() if isinstance(data.get('neighbors'), dict) else []:
                    if isinstance(nbr, list):
                        for n in nbr:
                            neighbors.append({
                                'neighbor_id': n.get('nbrNbridString', n.get('routerId', '')),
                                'address': n.get('ifaceAddress', ''),
                                'state': n.get('nbrState', ''),
                                'interface': n.get('ifaceName', ''),
                                'area': n.get('areaId', ''),
                                'priority': n.get('nbrPriority', ''),
                            })
                return DriverResult(success=True, data=neighbors)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(success=True, data=[])

    def get_environment(self):
        sensors, fans, psus = [], [], []
        try:
            output = self._cli_output('show platform temperature')
            if isinstance(output, str):
                for line in output.strip().splitlines()[2:]:
                    parts = line.split()
                    if len(parts) >= 2:
                        sensors.append({
                            'name': parts[0], 'value': parts[1] if len(parts) > 1 else '',
                            'type': 'temperature',
                            'status': 'ok' if len(parts) < 4 or parts[-1].lower() in ('ok', 'true') else 'warning',
                        })
        except Exception:
            pass
        try:
            output = self._cli_output('show platform fan')
            if isinstance(output, str):
                for line in output.strip().splitlines()[2:]:
                    parts = line.split()
                    if parts:
                        fans.append({'name': parts[0],
                                    'status': parts[-1].lower() if parts else 'unknown'})
        except Exception:
            pass
        try:
            output = self._cli_output('show platform psustatus')
            if isinstance(output, str):
                for line in output.strip().splitlines()[2:]:
                    parts = line.split()
                    if parts:
                        psus.append({'name': parts[0],
                                    'status': parts[-1].lower() if parts else 'unknown'})
        except Exception:
            pass
        return DriverResult(success=True, data={
            'sensors': sensors, 'fans': fans, 'power_supplies': psus,
        })

    def get_interface_counters(self):
        try:
            output = self._cli_output('show interfaces counters')
            counters = []
            if isinstance(output, str):
                lines = output.strip().splitlines()
                # Skip header lines
                for line in lines[2:]:
                    parts = line.split()
                    if len(parts) >= 5 and parts[0].startswith('Ethernet'):
                        try:
                            counters.append({
                                'name': parts[0],
                                'bytes_in': int(parts[1]) if parts[1].isdigit() else 0,
                                'bytes_out': int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0,
                                'packets_in': int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
                                'packets_out': int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0,
                                'errors_in': 0, 'errors_out': 0,
                            })
                        except (ValueError, IndexError):
                            pass
            return DriverResult(success=True, data=counters)
        except Exception as e:
            return DriverResult(error=str(e))

    def get_port_channel_members(self):
        """Get LAG members from OpenConfig interfaces aggregate-id, with CLI fallback."""
        try:
            result = {}
            # Primary: OpenConfig - each interface has aggregate-id pointing to its PC
            try:
                resp, _ = self._try_request('GET', '/restconf/data/openconfig-interfaces:interfaces')
                if resp and resp.status_code == 200:
                    intfs = resp.json().get('openconfig-interfaces:interfaces', {}).get('interface', [])
                    for intf in intfs:
                        name = intf.get('name', '')
                        # Check if this interface has an aggregate-id (belongs to a LAG)
                        eth = intf.get('openconfig-if-ethernet:ethernet', {})
                        cfg = eth.get('config', {})
                        agg_id = cfg.get('openconfig-if-aggregate:aggregate-id', '')
                        if agg_id:
                            pc_name = f'PortChannel{agg_id}' if not agg_id.startswith('PortChannel') else agg_id
                            result.setdefault(pc_name, []).append(name)
                    if result:
                        return DriverResult(success=True, data=result)
            except Exception:
                pass

            # Fallback: CLI
            output = self._cli_output('show interfaces portchannel')
            if isinstance(output, str) and output.strip():
                current_pc = None
                for line in output.splitlines():
                    m = re.match(r'(PortChannel\d+)', line)
                    if m:
                        current_pc = m.group(1)
                        result[current_pc] = []
                    elif current_pc and 'Ethernet' in line:
                        members = re.findall(r'(Ethernet\d+)', line)
                        result[current_pc].extend(members)
            return DriverResult(success=True, data=result)
        except Exception as e:
            return DriverResult(error=str(e))

    def get_dom_info(self):
        try:
            output = self._cli_output('show interfaces transceiver eeprom')
            dom_list = []
            if isinstance(output, str):
                current_intf = None
                for line in output.splitlines():
                    m = re.match(r'^(Ethernet\d+)', line)
                    if m:
                        current_intf = m.group(1)
                    elif current_intf and ':' in line:
                        key, _, val = line.partition(':')
                        key = key.strip().lower()
                        val = val.strip()
                        if 'temperature' in key or 'voltage' in key:
                            dom_list.append({
                                'interface': current_intf,
                                'media_type': '', 'vendor': '',
                                'rx_power': '', 'tx_power': '',
                                'temperature': val if 'temperature' in key else '',
                                'voltage': val if 'voltage' in key else '',
                            })
            return DriverResult(success=True, data=dom_list)
        except Exception as e:
            return DriverResult(error=str(e))

    @staticmethod
    def _intf_sort(name):
        m = re.match(r'Ethernet(\d+)', name)
        return int(m.group(1)) if m else 9999

    @staticmethod
    def _parse_speed(speed_str):
        if not speed_str:
            return 0
        s = str(speed_str).upper()
        if 'SPEED_100GB' in s or '100G' in s:
            return 100_000_000_000
        elif 'SPEED_40GB' in s or '40G' in s:
            return 40_000_000_000
        elif 'SPEED_25GB' in s or '25G' in s:
            return 25_000_000_000
        elif 'SPEED_10GB' in s or '10G' in s:
            return 10_000_000_000
        elif 'SPEED_1GB' in s or '1G' in s or '1000' in s:
            return 1_000_000_000
        elif '100M' in s or 'SPEED_100MB' in s:
            return 100_000_000
        try:
            return int(re.sub(r'[^\d]', '', s)) * 1_000_000
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _parse_uptime(boot_time_str):
        if not boot_time_str:
            return None
        try:
            from datetime import datetime
            boot = datetime.fromisoformat(boot_time_str.replace('Z', '+00:00'))
            return (datetime.now(boot.tzinfo) - boot).total_seconds()
        except Exception:
            return None

    # ---- Configuration Push ----

    def send_config(self, commands, commit=True):
        """Push config commands via SONiC CLI API or SSH fallback.

        Each command is sent verbatim; *commit* appends ``sudo config save -y``.
        ``data``: {outputs, errors, commands_sent}.
        """
        outputs = []
        errors = []
        for cmd in commands:
            try:
                output = self._cli_output(cmd, timeout=15)
                if not output:
                    output = self._ssh_run(cmd, timeout=15)
                outputs.append({'command': cmd, 'output': str(output)[:500]})
            except Exception as e:
                errors.append({'command': cmd, 'error': str(e)})
        if commit:
            try:
                out = self._cli_output('sudo config save -y', timeout=15)
                if not out:
                    out = self._ssh_run('sudo config save -y', timeout=15)
                outputs.append({'command': 'config save', 'output': 'ok' if out else ''})
            except Exception:
                pass
        return DriverResult(
            success=len(errors) == 0,
            data={'outputs': outputs, 'errors': errors, 'commands_sent': len(commands)},
            error='; '.join(e['error'] for e in errors) if errors else '',
        )

    def check_lldp_status(self):
        """Return dict: reachable_restconf, lldp_root_empty, interfaces_path_ok, cli_api_ok, ssh_ok, hint."""
        out = {
            'reachable_restconf': False,
            'lldp_root_empty': True,
            'interfaces_path_ok': False,
            'neighbor_count': 0,
            'cli_api_ok': False,
            'ssh_ok': False,
            'hint': '',
        }
        try:
            r, _ = self._try_request('GET', '/restconf/data/openconfig-system:system/state', timeout=8)
            out['reachable_restconf'] = bool(r and r.status_code == 200)
        except Exception:
            pass
        try:
            r, _ = self._try_request('GET', '/restconf/data/openconfig-lldp:lldp', timeout=10)
            if r and r.status_code == 200:
                out['lldp_root_empty'] = not (r.text and r.text.strip() not in ('', '{}'))
                if r.text and r.text.strip() not in ('', '{}'):
                    try:
                        data = r.json()
                        intfs = data.get('openconfig-lldp:lldp', data.get('openconfig-lldp:interfaces', {}))
                        if isinstance(intfs, dict) and 'interfaces' in intfs:
                            ilist = intfs['interfaces'].get('interface', [])
                            out['neighbor_count'] = sum(
                                len(i.get('neighbors', {}).get('neighbor', [])) for i in ilist)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            r, _ = self._try_request('GET', '/restconf/data/openconfig-lldp:lldp/interfaces', timeout=10)
            if r and r.status_code == 200 and r.text:
                data = r.json()
                intfs = data.get('openconfig-lldp:interfaces', {}).get('interface', [])
                out['interfaces_path_ok'] = True
                out['neighbor_count'] = sum(
                    len(i.get('neighbors', {}).get('neighbor', [])) for i in intfs)
        except Exception:
            pass
        try:
            session = _get_session(self.ip)
            url = f"{self._base_url('https')}/api/v1/cli"
            test = session.post(url, auth=self._auth(), json={'command': 'show version'}, verify=False, timeout=8)
            out['cli_api_ok'] = test.status_code == 200
        except Exception:
            pass
        try:
            ssh_out = self._ssh_run('show version', timeout=10)
            out['ssh_ok'] = bool(ssh_out and ('SONiC' in ssh_out or 'Version' in ssh_out))
        except Exception:
            pass
        if not out['interfaces_path_ok'] and not out['neighbor_count']:
            out['hint'] = (
                'LLDP not visible via RESTCONF. On the switch (SSH as admin), run: '
                'sudo config feature state lldp enabled && '
                'sudo config feature autorestart lldp enabled && '
                'sudo config save -y  then verify: show lldp table'
            )
        return out

    def enable_lldp(self):
        """Enable LLDP on SONiC. Uses RESTCONF to detect; applies config via CLI API when available."""
        details = []
        try:
            status = self.check_lldp_status()
            details.append(f"RESTCONF up: {status['reachable_restconf']}")
            details.append(f"LLDP REST root empty: {status['lldp_root_empty']}")
            details.append(f"LLDP /interfaces OK: {status['interfaces_path_ok']}")
            details.append(f"CLI /api/v1/cli OK: {status['cli_api_ok']}")
            details.append(f"SSH OK: {status['ssh_ok']}")
            if status['neighbor_count']:
                details.append(f"Neighbors seen via REST: {status['neighbor_count']}")
                return DriverResult(success=True, data={
                    'enabled_count': status['neighbor_count'],
                    'details': details,
                })

            # Already have interface table with neighbors
            if status['interfaces_path_ok'] and status['neighbor_count'] > 0:
                return DriverResult(success=True, data={
                    'enabled_count': status['neighbor_count'],
                    'details': details,
                })

            if status['cli_api_ok']:
                for cmd in ['sudo config feature state lldp enabled',
                            'sudo config feature autorestart lldp enabled',
                            'sudo config save -y']:
                    try:
                        output = self._cli_output(cmd, timeout=15)
                        details.append(f'{cmd}: {str(output)[:200]}')
                    except Exception as e:
                        details.append(f'{cmd}: {e}')
                return DriverResult(success=True, data={
                    'enabled_count': 1,
                    'details': details,
                })

            # Fallback: SSH when CLI API unavailable
            if status['ssh_ok']:
                for cmd in ['sudo config feature state lldp enabled',
                            'sudo config feature autorestart lldp enabled',
                            'sudo config save -y']:
                    try:
                        output = self._ssh_run(cmd, timeout=15)
                        details.append(f'SSH {cmd}: {str(output)[:200]}')
                    except Exception as e:
                        details.append(f'SSH {cmd}: {e}')
                return DriverResult(success=True, data={
                    'enabled_count': 1,
                    'details': details,
                })

            # No remote config path — CLI API and SSH both unavailable
            err = 'Cannot enable LLDP remotely: RESTCONF LLDP is empty, /api/v1/cli is not available, and SSH failed. '
            if not status['ssh_ok']:
                err += 'Verify device credentials (username/password) match SSH login.'
            else:
                err += 'Check device credentials.'
            return DriverResult(
                success=False,
                error=err,
                data={
                    'enabled_count': 0,
                    'details': details,
                    'ssh_steps': status['hint'] or (
                        'sudo config feature state lldp enabled && '
                        'sudo config feature autorestart lldp enabled && '
                        'sudo config save -y'
                    ),
                },
            )
        except Exception as e:
            return DriverResult(error=str(e))
