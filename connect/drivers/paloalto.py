"""
Palo Alto driver - communicates via PAN-OS XML API.
Uses API key authentication (generated from credentials or pre-configured).
Robust interface parsing: correlates ifnet (logical) and hw (physical) entries,
handles multi-vsys, and properly detects admin vs link status.

All calls are ``GET https://<host>:<api_port|443>/api/`` with ``type=op|config|commit``
and the API key as the ``key`` parameter. ``Device.api_key`` is used verbatim when
set; otherwise a key is generated once per IP via ``type=keygen`` (username/password)
and cached in-process. TLS verification disabled; first connect target only.
"""
import re
import logging
import requests
import xml.etree.ElementTree as ET
from urllib3.exceptions import InsecureRequestWarning

from .base import BaseDriver, DriverResult

logger = logging.getLogger(__name__)
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

_sessions = {}
_api_key_cache = {}


def _get_session(ip):
    if ip not in _sessions:
        s = requests.Session()
        s.verify = False
        _sessions[ip] = s
    return _sessions[ip]


def clear_cache(ip):
    _sessions.pop(ip, None)
    _api_key_cache.pop(ip, None)


class PaloAltoDriver(BaseDriver):
    """Palo Alto PAN-OS firewall driver over the XML API (op / config / commit)."""

    VENDOR_NAME = 'paloalto'

    def _base_url(self):
        port = self.api_port or 443
        return f"https://{self.ip}:{port}"

    def _get_api_key(self):
        """Stored ``api_key``, cached keygen result, or a fresh keygen (None on failure)."""
        if self.api_key:
            return self.api_key
        if self.ip in _api_key_cache:
            return _api_key_cache[self.ip]
        session = _get_session(self.ip)
        url = f"{self._base_url()}/api/"
        try:
            resp = session.get(url, params={
                'type': 'keygen', 'user': self.username, 'password': self.password
            }, timeout=10)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                key_elem = root.find('.//key')
                if key_elem is not None and key_elem.text:
                    _api_key_cache[self.ip] = key_elem.text
                    return key_elem.text
        except Exception as e:
            logger.debug(f"PAN-OS keygen error: {e}")
        return None

    def _api_call(self, params, timeout=12):
        key = self._get_api_key()
        if not key:
            return None
        session = _get_session(self.ip)
        params['key'] = key
        try:
            resp = session.get(f"{self._base_url()}/api/", params=params, timeout=timeout)
            return resp
        except Exception as e:
            logger.debug(f"PAN-OS API call error: {e}")
            return None

    def _parse_xml(self, resp):
        if resp is None or resp.status_code != 200:
            return None
        try:
            return ET.fromstring(resp.text)
        except ET.ParseError:
            return None

    def _text(self, elem, tag, default=''):
        """Safe text extraction from XML element."""
        if elem is None:
            return default
        el = elem.find(tag)
        return (el.text or '').strip() if el is not None and el.text else default

    def probe(self):
        """Keygen + ``show system info``. A connect failure during keygen is swallowed,
        so an unreachable firewall is usually reported as ``'auth_failed'``."""
        try:
            key = self._get_api_key()
            if not key:
                return 'auth_failed'
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><system><info></info></system></show>'
            }, timeout=8)
            if resp and resp.status_code == 200:
                root = self._parse_xml(resp)
                if root is not None:
                    return 'ok'
            return 'auth_failed'
        except requests.exceptions.SSLError:
            return 'unreachable'
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            return 'unreachable'
        except Exception:
            return 'unreachable'

    def get_system_info(self):
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><system><info></info></system></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                result = root.find('.//result/system')
                if result is None:
                    result = root.find('.//system')
                if result is None:
                    result = root.find('.//result')
                if result is not None:
                    uptime_secs = self._parse_uptime(self._text(result, 'uptime', '0'))
                    multi_vsys = self._text(result, 'multi-vsys', 'off')
                    return DriverResult(success=True, data={
                        'hostname': self._text(result, 'hostname', self.ip),
                        'version': self._text(result, 'sw-version'),
                        'serial_number': self._text(result, 'serial'),
                        'model_name': self._text(result, 'model', 'Palo Alto'),
                        'mac_address': self._text(result, 'mac-address'),
                        'uptime': uptime_secs,
                        'vendor_detail': f"PAN-OS {self._text(result, 'sw-version')} | Multi-vsys: {multi_vsys}",
                    })
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve system info')

    def get_interfaces(self):
        """Robust interface parsing for PA chassis (PA-7000/5400/3400 series) and
        VM/branch platforms. Uses three data sources:
        1) 'show interface all' -> ifnet (operational) + hw (physical link)
        2) 'show interface hardware' -> physical port info with link speed/state (more reliable for chassis)
        We merge hw + ifnet, hw providing physical link state and ifnet providing logical config/status.
        """
        try:
            physical, vlans, tunnels, mgmt, lags = [], [], [], [], []
            seen_names = set()

            # ---- Source 1: 'show interface all' (ifnet + hw) ----
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><interface>all</interface></show>'
            })
            root = self._parse_xml(resp)

            # ---- Source 2: 'show interface hardware' for reliable speed/state on chassis ----
            hw_resp = self._api_call({
                'type': 'op',
                'cmd': '<show><interface><hardware></hardware></interface></show>'
            })
            hw_root = self._parse_xml(hw_resp)

            # Build hw status map from all available sources
            hw_map = {}  # name -> {link_up, speed_mbps, duplex, mac, media, slot}

            # Parse hardware source (more reliable for chassis models)
            if hw_root is not None:
                for hw in hw_root.findall('.//entry'):
                    name = self._text(hw, 'name')
                    if not name:
                        name = hw.get('name', '')
                    if not name:
                        continue

                    # State field varies: 'state', 'st', 'link-state'
                    st_raw = self._text(hw, 'state') or self._text(hw, 'st') or self._text(hw, 'link-state', '0')
                    speed_raw = self._text(hw, 'speed', '0')
                    duplex = self._text(hw, 'duplex', '')
                    mac = self._text(hw, 'mac', '')
                    media = self._text(hw, 'media', self._text(hw, 'type', ''))
                    mode = self._text(hw, 'mode', '')

                    hw_up = self._parse_pa_link_state(st_raw, speed_raw)
                    speed_mbps = self._parse_pa_speed(speed_raw)

                    # Slot detection for chassis
                    slot = ''
                    m = re.match(r'ethernet(\d+)/', name)
                    if m:
                        slot = m.group(1)

                    hw_map[name] = {
                        'link_up': hw_up, 'speed_mbps': speed_mbps,
                        'duplex': duplex, 'mac': mac, 'media': media,
                        'slot': slot, 'mode': mode,
                    }

            # Parse 'show interface all' hw entries
            if root is not None:
                for hw in root.findall('.//hw/entry'):
                    name = self._text(hw, 'name')
                    if not name or name in hw_map:
                        continue
                    st_raw = self._text(hw, 'st', '0')
                    speed_raw = self._text(hw, 'speed', '0')
                    duplex = self._text(hw, 'duplex', '')
                    mac = self._text(hw, 'mac', '')
                    hw_up = self._parse_pa_link_state(st_raw, speed_raw)
                    speed_mbps = self._parse_pa_speed(speed_raw)
                    slot = ''
                    m = re.match(r'ethernet(\d+)/', name)
                    if m:
                        slot = m.group(1)
                    hw_map[name] = {
                        'link_up': hw_up, 'speed_mbps': speed_mbps,
                        'duplex': duplex, 'mac': mac, 'media': '',
                        'slot': slot, 'mode': '',
                    }

            # Parse ifnet entries (logical/operational interfaces)
            if root is not None:
                for intf in root.findall('.//ifnet/entry'):
                    name = self._text(intf, 'name')
                    if not name or name in seen_names:
                        continue
                    seen_names.add(name)

                    # Operational status from ifnet
                    status_raw = self._text(intf, 'status', 'down')
                    link = 'up' if status_raw.lower() in ('up', '10000', '1000', '100', '1') else 'down'

                    # Admin status
                    admin_raw = self._text(intf, 'admin', '')
                    admin_status = 'up'
                    if admin_raw and admin_raw.lower() in ('down', 'no', '0', 'disabled'):
                        admin_status = 'disabled'

                    # Correlate with hw_map for this interface
                    hw_info = hw_map.get(name, {})

                    # For physical link: if hw says up, use it (more reliable for chassis)
                    # But if admin_status is disabled, mark as down regardless
                    if hw_info.get('link_up') and admin_status == 'up':
                        link = 'up'

                    # Speed
                    speed_mbps = hw_info.get('speed_mbps', 0)
                    if speed_mbps == 0:
                        speed_text = self._text(intf, 'speed', '0')
                        speed_mbps = self._parse_pa_speed(speed_text)
                    bw = speed_mbps * 1_000_000

                    ip_addr = self._text(intf, 'ip', '')
                    zone = self._text(intf, 'zone', '')
                    vsys_str = self._text(intf, 'vsys', '')
                    fwd = self._text(intf, 'fwd', '')

                    desc_parts = []
                    if ip_addr and ip_addr != 'N/A':
                        desc_parts.append(ip_addr)
                    if zone:
                        desc_parts.append(f"zone:{zone}")
                    if vsys_str and vsys_str not in ('1', ''):
                        desc_parts.append(f"vsys:{vsys_str}")
                    media = hw_info.get('media', '')
                    if media:
                        desc_parts.append(media)
                    description = ' | '.join(desc_parts)

                    slot = hw_info.get('slot', '')
                    if not slot:
                        m = re.match(r'ethernet(\d+)/', name)
                        if m:
                            slot = m.group(1)

                    entry = {
                        'name': name,
                        'short_name': self._short_name(name),
                        'status': link,
                        'admin_status': admin_status,
                        'description': description,
                        'status_color': self._status_color(link, admin_status),
                        'bandwidth': bw, 'speed_label': self._speed_label(bw),
                        'mtu': self._text(intf, 'mtu', ''),
                        'mac': hw_info.get('mac', '') or self._text(intf, 'mac', ''),
                        'zone': zone, 'vsys': vsys_str,
                        'slot': slot, 'media': media,
                    }
                    lower = name.lower()
                    if 'vlan' in lower or ('.' in name and 'ethernet' in lower):
                        vlans.append(entry)
                    elif any(x in lower for x in ('tunnel', 'loopback')):
                        tunnels.append(entry)
                    elif 'management' in lower or 'mgmt' in lower:
                        mgmt.append(entry)
                    elif lower.startswith('ae') or lower.startswith('lag'):
                        lags.append(entry)  # Aggregate/LAG
                    else:
                        physical.append(entry)

            # Add hw-only entries (physical ports not in ifnet = unconfigured)
            for name, hw_info in hw_map.items():
                if name in seen_names:
                    continue
                seen_names.add(name)
                link = 'up' if hw_info['link_up'] else 'down'
                speed_mbps = hw_info['speed_mbps']
                bw = speed_mbps * 1_000_000
                admin = 'up'  # hw-present means admin not shut explicitly
                slot = hw_info.get('slot', '')
                media = hw_info.get('media', '')

                desc_parts = []
                if media:
                    desc_parts.append(media)
                if hw_info.get('mode'):
                    desc_parts.append(hw_info['mode'])
                description = ' | '.join(desc_parts) if desc_parts else 'unconfigured'

                entry = {
                    'name': name,
                    'short_name': self._short_name(name),
                    'status': link, 'admin_status': admin,
                    'description': description,
                    'status_color': self._status_color(link, admin),
                    'bandwidth': bw, 'speed_label': self._speed_label(bw),
                    'mtu': '', 'mac': hw_info.get('mac', ''),
                    'slot': slot, 'media': media,
                }
                lower = name.lower()
                if 'mgmt' in lower or 'management' in lower:
                    mgmt.append(entry)
                else:
                    physical.append(entry)

            # Sort physical ports: by slot, then port number
            def _pa_sort(e):
                m = re.match(r'ethernet(\d+)/(\d+)', e['name'])
                if m:
                    return (int(m.group(1)), int(m.group(2)))
                return (999, 0)
            physical.sort(key=_pa_sort)

            return DriverResult(success=True, data={
                'physical_data': physical, 'vlan_data': vlans,
                'port_channel_data': lags, 'management_data': mgmt,
                'tunnel_data': tunnels,
            })
        except Exception as e:
            return DriverResult(error=str(e))

    @staticmethod
    def _parse_pa_link_state(st_raw, speed_raw='0'):
        """Parse Palo Alto link state from various format fields.
        Values seen: '1'=up, '0'=down, 'up', 'down', '[e]'=up(error), speed as status."""
        if not st_raw:
            return False
        s = str(st_raw).lower().strip()
        if s in ('1', 'up', 'autoneg'):
            return True
        if s.startswith('['):
            return True  # '[e]' means up with errors
        # Some PA models put the speed in the state field
        if s.isdigit() and int(s) > 1:
            return True
        # Check speed as fallback
        try:
            spd = int(re.sub(r'[^\d]', '', str(speed_raw)) or '0')
            if spd > 0:
                return True
        except ValueError:
            pass
        return False

    @staticmethod
    def _parse_pa_speed(speed_str):
        """Parse PA speed to Mbps. Handles: '10000', '1000full', '10G', 'auto', 'unknown'."""
        if not speed_str:
            return 0
        s = str(speed_str).lower().strip()
        if s in ('auto', 'unknown', 'ukn', '', 'n/a', 'none'):
            return 0
        m = re.search(r'(\d+)', s)
        if not m:
            return 0
        val = int(m.group(1))
        if 'g' in s and val < 1000:
            return val * 1000
        return val

    def get_health(self):
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><system><resources></resources></system></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                text = root.findtext('.//result', '')
                cpu_util, mem_percent = 0, 0
                mem_total, mem_used = 0, 0
                for line in text.splitlines():
                    if 'Cpu(s)' in line or '%Cpu' in line:
                        parts = line.split(',')
                        for part in parts:
                            p = part.strip()
                            if 'id' in p.lower():
                                try:
                                    idle = float(re.search(r'([\d.]+)', p).group(1))
                                    cpu_util = round(100 - idle, 1)
                                except (ValueError, AttributeError):
                                    pass
                    if line.strip().startswith('Mem') or 'KiB Mem' in line or 'MiB Mem' in line:
                        numbers = re.findall(r'(\d+)', line)
                        if len(numbers) >= 2:
                            try:
                                mem_total = int(numbers[0])
                                mem_used = int(numbers[1])
                            except (ValueError, IndexError):
                                pass
                if mem_total:
                    mem_percent = round(mem_used / mem_total * 100, 1)
                return DriverResult(success=True, data={
                    'cpu_utilization': cpu_util, 'memory_used': mem_used,
                    'memory_total': mem_total, 'memory_percent': mem_percent,
                    'uptime': 0, 'temperature': None,
                })
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve health')

    def get_routes(self):
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><routing><route></route></routing></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                routes = []
                for entry in root.findall('.//entry'):
                    routes.append({
                        'vrf': self._text(entry, 'virtual-router', 'default'),
                        'prefix': self._text(entry, 'destination', ''),
                        'protocol': self._text(entry, 'type', ''),
                        'next_hop': self._text(entry, 'nexthop', 'directly connected'),
                        'interface': self._text(entry, 'interface', ''),
                        'metric': self._text(entry, 'metric', ''),
                        'preference': self._text(entry, 'admin-dist', ''),
                    })
                return DriverResult(success=True, data=routes)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve routes')

    def get_vlans(self):
        return DriverResult(success=True, data=[])

    def get_running_config(self):
        try:
            resp = self._api_call({'type': 'config', 'action': 'show'}, timeout=20)
            if resp and resp.status_code == 200:
                return DriverResult(success=True, data=resp.text)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve config')

    def get_startup_config(self):
        return self.get_running_config()

    def execute_command(self, command):
        """``show ...`` only; converted to nested XML tags by :meth:`_cli_to_xml`."""
        cmd = command.strip()
        if not cmd.lower().startswith('show'):
            return DriverResult(error="Only 'show' commands are allowed.")
        xml_cmd = self._cli_to_xml(cmd)
        try:
            resp = self._api_call({'type': 'op', 'cmd': xml_cmd}, timeout=15)
            root = self._parse_xml(resp)
            if root is not None:
                result_elem = root.find('.//result')
                if result_elem is not None:
                    text = ET.tostring(result_elem, encoding='unicode', method='text')
                    if text and text.strip():
                        return DriverResult(success=True, data=text)
                    return DriverResult(success=True, data=ET.tostring(result_elem, encoding='unicode'))
            return DriverResult(success=True, data=resp.text if resp else 'No response')
        except Exception as e:
            return DriverResult(error=str(e))

    def get_arp_table(self):
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><arp><entry name = "all"/></arp></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                entries = []
                for entry in root.findall('.//entry'):
                    entries.append({
                        'ip': self._text(entry, 'ip'),
                        'mac': self._text(entry, 'mac'),
                        'interface': self._text(entry, 'interface'),
                        'age': self._text(entry, 'ttl'),
                    })
                if not entries:
                    for entry in root.findall('.//entries'):
                        entries.append({
                            'ip': self._text(entry, 'ip'),
                            'mac': self._text(entry, 'mac'),
                            'interface': self._text(entry, 'interface'),
                            'age': self._text(entry, 'ttl'),
                        })
                return DriverResult(success=True, data=entries)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve ARP table')

    def get_mac_table(self):
        return DriverResult(success=True, data=[])

    def get_lldp_neighbors(self):
        """Fetch LLDP neighbors.
        PA XML structure:
          <entry name="ethernet2/27">     <-- local_port from @name
            <local>...</local>
            <neighbors>
              <entry name="...hex...">    <-- neighbor entry
                <chassis-id>...</chassis-id>
                <port-id>Ethernet23/1</port-id>
                <system-name>str-7260-10</system-name>
                <management-address>
                  <entry name="192.0.2.91">
                    <address-type>ipv4</address-type>
                  </entry>
                </management-address>
              </entry>
            </neighbors>
          </entry>
        """
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><lldp><neighbors>all</neighbors></lldp></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                neighbors = []
                # Top-level entries are local interfaces
                for intf_entry in root.findall('.//result/entry'):
                    local_port = intf_entry.get('name', '')  # e.g. "ethernet2/27"
                    # Neighbor entries are under <neighbors>/<entry>
                    for nbr in intf_entry.findall('neighbors/entry'):
                        chassis_id = self._text(nbr, 'chassis-id')
                        remote_port = self._text(nbr, 'port-id') or self._text(nbr, 'port-description')
                        remote_device = self._text(nbr, 'system-name') or chassis_id
                        # Management address: <management-address>/<entry name="10.x.x.x">
                        mgmt_ip = ''
                        mgmt_entries = nbr.findall('management-address/entry')
                        for me in mgmt_entries:
                            addr_type = self._text(me, 'address-type')
                            if addr_type == 'ipv4':
                                mgmt_ip = me.get('name', '')
                                break
                        if not mgmt_ip and mgmt_entries:
                            mgmt_ip = mgmt_entries[0].get('name', '')
                        neighbors.append({
                            'local_port': local_port,
                            'remote_device': remote_device,
                            'remote_port': remote_port,
                            'chassis_id': chassis_id,
                            'mgmt_ip': mgmt_ip,
                            'system_description': self._text(nbr, 'system-description'),
                        })
                return DriverResult(success=True, data=neighbors)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve LLDP neighbors')

    def get_lldp_neighbors_detail(self):
        return self.get_lldp_neighbors()

    def get_security_policies(self):
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><running><security-policy></security-policy></running></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                policies = []
                for entry in root.findall('.//entry'):
                    def _members(path):
                        members = entry.findall(f'{path}/member')
                        return ', '.join(m.text for m in members if m.text) if members else self._text(entry, f'{path}/member')
                    action_el = entry.find('.//action')
                    action = ''
                    if action_el is not None:
                        if len(action_el) > 0:
                            action = action_el[0].tag
                        elif action_el.text:
                            action = action_el.text.strip()
                    policies.append({
                        'id': entry.get('name', ''),
                        'name': entry.get('name', ''),
                        'srcintf': _members('.//from'),
                        'dstintf': _members('.//to'),
                        'srcaddr': _members('.//source'),
                        'dstaddr': _members('.//destination'),
                        'service': _members('.//service'),
                        'action': action,
                        'status': 'enabled' if not self._text(entry, './/disabled') else 'disabled',
                        'log': self._text(entry, './/log-setting'),
                    })
                return DriverResult(success=True, data=policies)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve policies')

    def get_vpn_tunnels(self):
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><vpn><ipsec-sa></ipsec-sa></vpn></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                tunnels = []
                for entry in root.findall('.//entry'):
                    tunnels.append({
                        'name': self._text(entry, 'name'),
                        'type': 'IPSec',
                        'remote_gw': self._text(entry, 'gwid') or self._text(entry, 'gateway'),
                        'status': self._text(entry, 'state', 'down').lower(),
                        'incoming_bytes': int(self._text(entry, 'inbytes', '0') or '0'),
                        'outgoing_bytes': int(self._text(entry, 'outbytes', '0') or '0'),
                    })
                return DriverResult(success=True, data=tunnels)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve VPN tunnels')

    def get_ha_status(self):
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><high-availability><state></state></high-availability></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                result = root.find('.//result')
                if result is not None:
                    return DriverResult(success=True, data={
                        'enabled': self._text(result, 'enabled', 'no'),
                        'state': self._text(result, './/group/local-info/state'),
                        'peer_state': self._text(result, './/group/peer-info/state'),
                    })
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(error='Cannot retrieve HA status')

    # ---- New driver methods for enhanced features ----

    def get_bgp_summary(self):
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><routing><protocol><bgp><peer></peer></bgp></protocol></routing></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                peers = []
                for entry in root.findall('.//entry'):
                    peers.append({
                        'neighbor': self._text(entry, 'peer') or entry.get('name', ''),
                        'asn': self._text(entry, 'remote-as'),
                        'state': self._text(entry, 'status', 'unknown'),
                        'prefixes_received': int(self._text(entry, 'prefix-counter', '0') or '0'),
                        'uptime': self._text(entry, 'elapsed-time'),
                        'vrf': self._text(entry, 'virtual-router', 'default'),
                    })
                return DriverResult(success=True, data=peers)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(success=True, data=[])

    def get_ospf_neighbors(self):
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><routing><protocol><ospf><neighbor></neighbor></ospf></protocol></routing></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                neighbors = []
                for entry in root.findall('.//entry'):
                    neighbors.append({
                        'neighbor_id': self._text(entry, 'neighbor-id') or entry.get('name', ''),
                        'address': self._text(entry, 'neighbor-address'),
                        'state': self._text(entry, 'state'),
                        'interface': self._text(entry, 'interface-name'),
                        'area': self._text(entry, 'area-id'),
                        'priority': self._text(entry, 'priority'),
                    })
                return DriverResult(success=True, data=neighbors)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(success=True, data=[])

    def get_environment(self):
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><system><environmentals></environmentals></system></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                text = root.findtext('.//result', '')
                sensors, fans, psus = [], [], []
                current_section = None
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if 'Thermal' in line or 'Temperature' in line:
                        current_section = 'temp'
                    elif 'Fan' in line:
                        current_section = 'fan'
                    elif 'Power' in line:
                        current_section = 'psu'
                    elif current_section == 'temp' and line and not line.startswith('-'):
                        parts = line.split()
                        if len(parts) >= 2:
                            sensors.append({'name': parts[0], 'value': ' '.join(parts[1:]),
                                          'type': 'temperature', 'status': 'ok'})
                    elif current_section == 'fan' and line and not line.startswith('-'):
                        fans.append({'name': line.split()[0] if line.split() else line,
                                    'status': 'ok' if 'True' in line or 'OK' in line else 'warning'})
                    elif current_section == 'psu' and line and not line.startswith('-'):
                        psus.append({'name': line.split()[0] if line.split() else line,
                                    'status': 'ok' if 'True' in line or 'OK' in line else 'warning'})
                return DriverResult(success=True, data={
                    'sensors': sensors, 'fans': fans, 'power_supplies': psus,
                })
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(success=True, data={'sensors': [], 'fans': [], 'power_supplies': []})

    def get_interface_counters(self):
        try:
            resp = self._api_call({
                'type': 'op',
                'cmd': '<show><counter><interface>all</interface></counter></show>'
            })
            root = self._parse_xml(resp)
            if root is not None:
                counters = []
                for entry in root.findall('.//entry') or root.findall('.//ifnet/entry'):
                    counters.append({
                        'name': self._text(entry, 'name') or entry.get('name', ''),
                        'bytes_in': int(self._text(entry, 'ibytes', '0') or '0'),
                        'bytes_out': int(self._text(entry, 'obytes', '0') or '0'),
                        'packets_in': int(self._text(entry, 'ipackets', '0') or '0'),
                        'packets_out': int(self._text(entry, 'opackets', '0') or '0'),
                        'errors_in': int(self._text(entry, 'ierrors', '0') or '0'),
                        'errors_out': int(self._text(entry, 'oerrors', '0') or '0'),
                    })
                return DriverResult(success=True, data=counters)
        except Exception as e:
            return DriverResult(error=str(e))
        return DriverResult(success=True, data=[])

    def get_port_channel_members(self):
        return DriverResult(success=True, data={})

    def get_dom_info(self):
        return DriverResult(success=True, data=[])

    # ---- Configuration Push ----

    def send_config(self, commands, commit=True):
        """Push config via PAN-OS XML API. Each command is a dict:
        {'xpath': '...', 'element': '...'} for config set operations,
        or a string for op-mode commands."""
        key = self._get_api_key()
        if not key:
            return DriverResult(error='No API key available')

        session = _get_session(self.ip)
        outputs = []
        errors = []

        for cmd in commands:
            try:
                if isinstance(cmd, dict):
                    resp = session.get(f"{self._base_url()}/api/", params={
                        'key': key, 'type': 'config', 'action': 'set',
                        'xpath': cmd['xpath'], 'element': cmd['element'],
                    }, timeout=15)
                    root = self._parse_xml(resp)
                    status = root.get('status', 'error') if root is not None else 'error'
                    msg = root.findtext('.//msg', '') if root is not None else resp.text[:200]
                    outputs.append({'xpath': cmd['xpath'][:80], 'status': status, 'msg': msg})
                    if status != 'success':
                        errors.append({'xpath': cmd['xpath'][:80], 'error': msg})
                else:
                    # Op-mode command
                    xml_cmd = self._cli_to_xml(cmd) if not cmd.startswith('<') else cmd
                    resp = session.get(f"{self._base_url()}/api/", params={
                        'key': key, 'type': 'op', 'cmd': xml_cmd,
                    }, timeout=15)
                    outputs.append({'command': cmd[:80], 'status': resp.status_code})
            except Exception as e:
                errors.append({'command': str(cmd)[:80], 'error': str(e)})

        # Commit if requested and there were no errors
        if commit and len(errors) == 0:
            try:
                resp = session.get(f"{self._base_url()}/api/", params={
                    'key': key, 'type': 'commit', 'cmd': '<commit></commit>',
                }, timeout=120)
                root = self._parse_xml(resp)
                job_id = root.findtext('.//job', '') if root is not None else ''
                outputs.append({'action': 'commit', 'job_id': job_id})
            except Exception as e:
                errors.append({'action': 'commit', 'error': str(e)})

        return DriverResult(
            success=len(errors) == 0,
            data={'outputs': outputs, 'errors': errors, 'commands_sent': len(commands)},
            error='; '.join(str(e) for e in errors) if errors else '',
        )

    def enable_lldp(self):
        """Enable LLDP globally and on all physical interfaces (per-interface mode-aware)."""
        key = self._get_api_key()
        if not key:
            return DriverResult(error='No API key available')

        session = _get_session(self.ip)
        details = []
        base_xpath = "/config/devices/entry[@name='localhost.localdomain']"

        # 1) Enable LLDP globally
        resp = session.get(f"{self._base_url()}/api/", params={
            'key': key, 'type': 'config', 'action': 'set',
            'xpath': f"{base_xpath}/network/lldp",
            'element': '<enable>yes</enable>',
        }, timeout=10)
        root = self._parse_xml(resp)
        status = root.get('status', '?') if root is not None else '?'
        details.append(f'Global LLDP enable: {status}')

        # 2) Get interface config to determine mode per interface
        resp = session.get(f"{self._base_url()}/api/", params={
            'key': key, 'type': 'config', 'action': 'get',
            'xpath': f"{base_xpath}/network/interface/ethernet",
        }, timeout=15)
        root = self._parse_xml(resp)
        ethernet = root.find('.//ethernet') if root is not None else None

        enabled_count = 0
        if ethernet is not None:
            for entry in ethernet.findall('entry'):
                name = entry.get('name', '')
                intf_xpath = f"{base_xpath}/network/interface/ethernet/entry[@name='{name}']"

                # Determine interface mode
                if entry.find('layer3') is not None:
                    mode_path = 'layer3'
                elif entry.find('virtual-wire') is not None:
                    mode_path = 'virtual-wire'
                elif entry.find('layer2') is not None:
                    mode_path = 'layer2'
                else:
                    details.append(f'{name}: skipped (no mode configured)')
                    continue

                try:
                    resp = session.get(f"{self._base_url()}/api/", params={
                        'key': key, 'type': 'config', 'action': 'set',
                        'xpath': f"{intf_xpath}/{mode_path}/lldp",
                        'element': '<enable>yes</enable>',
                    }, timeout=10)
                    root2 = self._parse_xml(resp)
                    if root2 is not None and root2.get('status') == 'success':
                        enabled_count += 1
                    else:
                        details.append(f'{name} ({mode_path}): failed')
                except Exception as e:
                    details.append(f'{name}: {e}')

        details.insert(0, f'LLDP enabled on {enabled_count} interfaces')

        # 3) Commit
        try:
            resp = session.get(f"{self._base_url()}/api/", params={
                'key': key, 'type': 'commit', 'cmd': '<commit></commit>',
            }, timeout=120)
            root = self._parse_xml(resp)
            job_id = root.findtext('.//job', '') if root is not None else ''
            details.append(f'Commit job: {job_id}')
        except Exception as e:
            details.append(f'Commit error: {e}')

        return DriverResult(success=enabled_count > 0, data={
            'enabled_count': enabled_count,
            'details': details,
        })

    @staticmethod
    def _cli_to_xml(cli_cmd):
        """``'show system info'`` → ``'<show><system><info></info></system></show>'``
        (non ``[A-Za-z0-9_-]`` characters stripped from each token)."""
        parts = cli_cmd.strip().split()
        xml = ''
        closing = ''
        for part in parts:
            safe = re.sub(r'[^a-zA-Z0-9_-]', '', part)
            if safe:
                xml += f'<{safe}>'
                closing = f'</{safe}>' + closing
        return xml + closing

    @staticmethod
    def _parse_uptime(uptime_str):
        if not uptime_str:
            return 0
        total = 0
        try:
            if 'day' in uptime_str:
                parts = uptime_str.split(',', 1)
                day_part = parts[0].strip()
                days = int(re.search(r'(\d+)', day_part).group(1))
                total += days * 86400
                time_part = parts[1].strip() if len(parts) > 1 else ''
            else:
                time_part = uptime_str.strip()
            if ':' in time_part:
                tp = time_part.split(':')
                total += int(tp[0]) * 3600
                if len(tp) > 1:
                    total += int(tp[1]) * 60
                if len(tp) > 2:
                    total += int(tp[2])
        except (ValueError, IndexError, AttributeError):
            pass
        return total
