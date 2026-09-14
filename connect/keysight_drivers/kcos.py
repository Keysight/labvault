"""
KCOS REST API Driver for Keysight APS chassis (APS-M1010, APS-M8400, etc.).

Authentication : Keycloak OAuth2 password grant
                 POST /auth/realms/keysight/protocol/openid-connect/token
Base URL       : https://{ip}/api/v2
Swagger docs   : https://{ip}/restapi/swagger-ui/index.html

KCOS (Keysight Cluster Operating System) is a Kubernetes-based platform.
Nodes map to physical blades/slots; connections map to front-panel ports.
Each compute node runs an "app" (e.g. BreakingPoint, IxLoad) that can be
switched at runtime.

Ownership (M8400 front-panel + APS compute node ports):
  Ownership is read from KCOS introspection APIs only (not BPS).
  Supported fields: owner, reservedBy, assignedTo, reservedFor, team, tenant.
  Example /introspection/connections response:
    {"tepid":"1.0","name":"merpro2a","link":"UP","owner":"QA-Team","slot":1,"to":"1.0",...}
    {"nodeName":"merpro2b","reservedBy":"Apps-Lab","link":"DOWN",...}
  Fallbacks: /introspection/logicalports, /introspection/debug/hardware/frontpanel.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import requests
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

logger = logging.getLogger(__name__)

from ..ip_addressing import bracket_host, unbracket_host

from ..ip_addressing import bracket_host, unbracket_host

from ..ip_addressing import bracket_host

# Re-use DriverResult from ixos module for consistent interface
from .ixos import DriverResult


# ---------------------------------------------------------------------------
# Token cache  (access_token per IP so we don't re-auth every call)
# ---------------------------------------------------------------------------
_token_cache: dict[str, dict] = {}   # ip -> {'token': str, 'expires_at': float}
_session_pool: dict[str, requests.Session] = {}


def _get_session(ip: str) -> requests.Session:
    if ip not in _session_pool:
        s = requests.Session()
        s.verify = False
        # Increase pool size to handle parallel API calls
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        s.mount('https://', adapter)
        s.mount('http://', adapter)
        _session_pool[ip] = s
    return _session_pool[ip]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class KCOSDriver:
    """Driver for a single KCOS-based APS chassis."""

    # Keycloak settings (standard across all KCOS deployments)
    _REALM = 'keysight'
    _CLIENT_ID = 'kcos-rest-api'
    _TOKEN_BUFFER = 30  # refresh token this many seconds before expiry

    def __init__(self, ip: str, username: str = 'admin', password: str = 'admin'):
        self.ip = bracket_host((ip or '').strip())
        self.username = username
        self.password = password
        self._token: str | None = None
        self._token_expires_at: float = 0
        # Per-request endpoint cache — avoids duplicate HTTP calls within
        # a single fetch_chassis_data() cycle (thread-safe)
        self._req_cache: dict[str, DriverResult] = {}
        self._req_lock = threading.Lock()
        # Restore from cache
        cached = _token_cache.get(ip, {})
        if cached.get('expires_at', 0) > time.time():
            self._token = cached['token']
            self._token_expires_at = cached['expires_at']

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _base_url(self) -> str:
        return f'https://{self.ip}/api/v2'

    @property
    def _token_url(self) -> str:
        return (f'https://{self.ip}/auth/realms/{self._REALM}'
                f'/protocol/openid-connect/token')

    def _headers(self) -> dict:
        return {
            'Authorization': f'Bearer {self._token or ""}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

    def _authenticate(self) -> bool:
        """Obtain a Keycloak access token via password grant."""
        session = _get_session(self.ip)
        try:
            resp = session.post(self._token_url, data={
                'grant_type': 'password',
                'client_id': self._CLIENT_ID,
                'username': self.username,
                'password': self.password,
            }, verify=False, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                self._token = data.get('access_token', '')
                expires_in = data.get('expires_in', 300)
                self._token_expires_at = time.time() + expires_in - self._TOKEN_BUFFER
                if self._token:
                    _token_cache[self.ip] = {
                        'token': self._token,
                        'expires_at': self._token_expires_at,
                    }
                    return True
            logger.debug('KCOS auth failed for %s: HTTP %s', self.ip, resp.status_code)
            return False
        except Exception as e:
            logger.debug('KCOS auth error for %s: %s', self.ip, e)
            return False

    def _ensure_auth(self) -> bool:
        """Ensure we have a valid (non-expired) token."""
        if self._token and time.time() < self._token_expires_at:
            return True
        return self._authenticate()

    def _get(self, path: str, timeout: int = 15) -> DriverResult:
        """Issue a GET request with per-request dedup cache (thread-safe)."""
        with self._req_lock:
            if path in self._req_cache:
                return self._req_cache[path]

        if not self._ensure_auth():
            return DriverResult(error='Authentication failed')
        session = _get_session(self.ip)
        url = path if path.startswith('http') else self._base_url + path
        try:
            resp = session.get(url, headers=self._headers(),
                               verify=False, timeout=timeout)
            if resp.status_code == 401:
                self._token = None
                if self._authenticate():
                    resp = session.get(url, headers=self._headers(),
                                       verify=False, timeout=timeout)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = resp.text
                result = DriverResult(success=True, data=data)
            else:
                result = DriverResult(error=f'HTTP {resp.status_code}')
        except Exception as e:
            result = DriverResult(error=str(e))

        with self._req_lock:
            self._req_cache[path] = result
        return result

    def _post(self, path: str, payload: Any = None, timeout: int = 30) -> DriverResult:
        """Issue a POST request and return a DriverResult."""
        if not self._ensure_auth():
            return DriverResult(error='Authentication failed')
        session = _get_session(self.ip)
        url = path if path.startswith('http') else self._base_url + path
        try:
            resp = session.post(url, json=payload, headers=self._headers(),
                                verify=False, timeout=timeout)
            if resp.status_code == 401:
                self._token = None
                if self._authenticate():
                    resp = session.post(url, json=payload, headers=self._headers(),
                                        verify=False, timeout=timeout)
            if resp.status_code in (200, 202):
                try:
                    data = resp.json()
                except Exception:
                    data = resp.text
                return DriverResult(success=True, data=data)
            return DriverResult(error=f'HTTP {resp.status_code}')
        except Exception as e:
            return DriverResult(error=str(e))

    # ------------------------------------------------------------------
    # Public API — connectivity  (matches IxOSDriver interface)
    # ------------------------------------------------------------------

    def probe(self) -> str:
        """Test connectivity. Returns 'ok', 'auth_failed', or 'unreachable'."""
        try:
            if self._authenticate():
                result = self._get('/vital/hostname', timeout=10)
                if result.success:
                    return 'ok'
                return 'ok'  # auth worked even if hostname endpoint quirky
            return 'auth_failed'
        except Exception:
            return 'unreachable'

    # ------------------------------------------------------------------
    # Public API — chassis info  (matches IxOSDriver interface)
    # ------------------------------------------------------------------

    def get_chassis_info(self) -> DriverResult:
        """Fetch chassis-level details: hostname, KCOS version, node count."""
        # Hostname
        hostname_result = self._get('/vital/hostname')
        hostname = ''
        if hostname_result.success and isinstance(hostname_result.data, dict):
            hostname = hostname_result.data.get('name', '')

        # Nodes — needed for version, node count, etc.
        nodes_result = self._get('/introspection/nodes')
        nodes = nodes_result.data if nodes_result.success and isinstance(nodes_result.data, list) else []

        # Extract OS image from first node (for display, NOT the KCOS version)
        os_image = ''
        for n in nodes:
            os_img = n.get('osImage', '')
            if os_img:
                os_image = os_img
                break

        # Get actual KCOS version from deployment API
        deploy_info = self.get_deployment_info()
        kcos_version = ''
        kcos_chart_name = ''
        if deploy_info.success and isinstance(deploy_info.data, dict):
            kcos_version = deploy_info.data.get('kcos_version', '')
            kcos_chart_name = deploy_info.data.get('kcos_chart_name', '')
        if not kcos_version:
            # Fallback: use osImage if deployment API is unavailable
            kcos_version = os_image
        # Show deployed app (e.g. aps-kcos or aps-kcos-400) + version, not kernel
        if kcos_chart_name and kcos_version:
            kcos_version = f'{kcos_chart_name} {kcos_version}'.strip()

        # Count compute nodes (exclude mgmt)
        from ..keysight_aps_standalone import is_mgmt_kcos_role, is_standalone_kcos_api_nodes

        if is_standalone_kcos_api_nodes(nodes):
            compute_nodes = nodes
        else:
            compute_nodes = [n for n in nodes if not is_mgmt_kcos_role(n.get('role', ''))]

        # BMCs — for serial numbers
        bmcs_result = self._get('/introspection/bmcs')
        bmcs = bmcs_result.data if bmcs_result.success and isinstance(bmcs_result.data, list) else []
        serial = ''
        if bmcs:
            serial = bmcs[0].get('data', {}).get('serialNumber', '')

        info = {
            'management_ip': self.ip,
            'chassis_type': 'APS',
            'serial_number': serial,
            'controller_serial': '',
            'state': 'ready' if all(n.get('status') == 'Ready' for n in compute_nodes) else 'degraded',
            'num_physical_cards': len(compute_nodes),
            'ixos_applications': {},
            'ixos_version': '',
            # KCOS-specific
            'hostname': hostname,
            'kcos_version': kcos_version,
            'kcos_chart_name': kcos_chart_name,
            'os_image': os_image,
            'os_platform': 'kcos',
            'k8s_version': nodes[0].get('version', '') if nodes else '',
            'kernel_version': nodes[0].get('kernelVersion', '') if nodes else '',
            'total_nodes': len(nodes),
            'compute_nodes': len(compute_nodes),
        }
        return DriverResult(success=True, data=info)

    # ------------------------------------------------------------------
    # Public API — deployment info (KCOS version from Helm releases)
    # ------------------------------------------------------------------

    def get_deployment_info(self) -> DriverResult:
        """Fetch deployment info from Helm releases to get the actual
        KCOS version (chart version) and list of installed applications."""
        result = self._get('/deployment/helm/cluster/releases')
        if not result.success:
            return DriverResult(success=True, data={'kcos_version': '', 'kcos_chart_name': '', 'installed_apps': []})
        raw = result.data if isinstance(result.data, list) else []

        kcos_version = ''
        kcos_chart_name = ''
        installed_apps = []
        for release in raw:
            chart_dep = release.get('chartDeployment', {})
            if not chart_dep:
                continue
            visibility = chart_dep.get('visibilityType', '')
            display_name = chart_dep.get('displayName', '')
            chart_name = chart_dep.get('chartName', '')
            chart_version = chart_dep.get('chartVersion', '')
            chart_version_display = chart_dep.get('chartVersionDisplay', chart_version)
            last_deployed = chart_dep.get('lastDeployed', '')

            # KCOS version: use the main platform app (aps-kcos-400 or aps-kcos), not internal charts like kcos-weave-net
            if display_name in ('aps-kcos-400', 'aps-kcos'):
                kcos_version = chart_version_display or chart_version
                kcos_chart_name = display_name  # Application name for display e.g. aps-kcos-400

            if visibility == 'PUBLIC' and display_name:
                installed_apps.append({
                    'chart_name': chart_name,
                    'display_name': display_name,
                    'chart_version': chart_version,
                    'chart_version_display': chart_version_display,
                    'last_deployed': last_deployed,
                })

        return DriverResult(success=True, data={
            'kcos_version': kcos_version,
            'kcos_chart_name': kcos_chart_name,
            'installed_apps': installed_apps,
        })

    def get_deployed_apps(self) -> DriverResult:
        """Return the list of publicly visible deployed applications
        with their chart names, versions, and deployment timestamps."""
        deploy_info = self.get_deployment_info()
        if deploy_info.success and isinstance(deploy_info.data, dict):
            return DriverResult(success=True, data=deploy_info.data.get('installed_apps', []))
        return DriverResult(success=True, data=[])

    # ------------------------------------------------------------------
    # Public API — cards/slots (nodes)  (matches IxOSDriver interface)
    # ------------------------------------------------------------------

    def get_cards(self) -> DriverResult:
        """Fetch all compute nodes, mapped to the card/slot paradigm.
        Merges data from nodes, apps, and BMCs endpoints."""
        # Nodes
        nodes_result = self._get('/introspection/nodes')
        if not nodes_result.success:
            return nodes_result
        all_nodes = nodes_result.data if isinstance(nodes_result.data, list) else []

        # Apps (per node)
        apps_result = self._get('/introspection/apps')
        apps_by_node: dict[str, dict] = {}
        if apps_result.success and isinstance(apps_result.data, dict):
            for node_app in apps_result.data.get('nodes', []):
                apps_by_node[node_app.get('nodeName', '')] = node_app

        # BMCs
        bmcs_result = self._get('/introspection/bmcs')
        bmcs_by_node: dict[str, dict] = {}
        if bmcs_result.success and isinstance(bmcs_result.data, list):
            for bmc in bmcs_result.data:
                node_name = bmc.get('data', {}).get('nodeName', '')
                if node_name:
                    bmcs_by_node[node_name] = bmc

        # Connections (to count ports per node/slot)
        conns_result = self._get('/introspection/connections')
        conns = conns_result.data if conns_result.success and isinstance(conns_result.data, list) else []
        ports_per_node: dict[str, int] = {}
        for c in conns:
            node_name = c.get('name', '')
            ports_per_node[node_name] = ports_per_node.get(node_name, 0) + 1

        # Firmware
        fw_result = self._get('/firmware-controller/components')
        fw_by_node: dict[str, list] = {}
        if fw_result.success and isinstance(fw_result.data, list):
            for comp in fw_result.data:
                nname = comp.get('node-name', '')
                fw_by_node[nname] = comp.get('firmwares', [])

        cards = []
        for node in all_nodes:
            name = node.get('name', '')
            role = node.get('role', '')

            # Include mgmt node (role=merlin) as a special "Main Slot"
            is_mgmt = (role == 'merlin')

            app_info = apps_by_node.get(name, {})
            bmc_info = bmcs_by_node.get(name, {})
            slot_str = app_info.get('slotNumber', '0')
            try:
                slot = int(slot_str)
            except (ValueError, TypeError):
                slot = 0

            current_app = app_info.get('currentApp', {})
            available_apps = app_info.get('availableApps', [])
            app_status = app_info.get('appStatus', {})

            firmwares = fw_by_node.get(name, [])

            cards.append({
                'id': slot,  # use slot as ID (matches IxOS card.id)
                'card_number': slot,
                'type': name,
                'state': node.get('status', 'unknown').lower(),
                'serial_number': bmc_info.get('data', {}).get('serialNumber', ''),
                'num_ports': ports_per_node.get(name, 0),
                # KCOS-specific
                'is_mgmt_slot': is_mgmt,
                'os_image': node.get('osImage', ''),
                'kernel_version': node.get('kernelVersion', ''),
                'k8s_version': node.get('version', ''),
                'internal_ip': node.get('internalIP', ''),
                'container_runtime': node.get('containerRuntime', ''),
                'role': role,
                'node_name': name,
                'current_app': current_app.get('uiAppId', current_app.get('appId', '')),
                'current_app_id': current_app.get('appId', ''),
                'available_apps': [
                    {'app_id': a.get('appId', ''), 'ui_name': a.get('uiAppId', a.get('appId', ''))}
                    for a in available_apps
                ],
                'app_status': app_status.get('type', 'unknown'),
                'app_status_message': app_status.get('message', ''),
                'bmc_name': bmc_info.get('bmcName', ''),
                'bmc_power': bmc_info.get('power', 'unknown'),
                'firmwares': [
                    {
                        'name': fw.get('name', ''),
                        'type': fw.get('type', ''),
                        'version': fw.get('version', ''),
                        'update_available': fw.get('update-available', 'false'),
                        'update_version': fw.get('update-version', ''),
                    }
                    for fw in firmwares
                ],
            })

        # Sort: mgmt slot first, then by card_number
        cards.sort(key=lambda c: (0 if c.get('is_mgmt_slot') else 1, c['card_number']))
        return DriverResult(success=True, data=cards)

    # ------------------------------------------------------------------
    # Ownership extraction (KCOS introspection APIs)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_kcos_ownership(record: dict) -> str:
        """Extract owner from KCOS API record. Supports common field names from Swagger.
        Example API response:
            {"tepid": "1.0", "name": "merpro2a", "link": "UP", "owner": "QA-Team", ...}
            {"reservedBy": "Apps-Lab", "nodeName": "merpro2b", ...}
            {"assignedTo": "Regression", "reservedFor": "Perf", ...}
        Returns non-empty string or 'Free'."""
        if not isinstance(record, dict):
            return 'Free'
        for key in ('owner', 'reservedBy', 'assignedTo', 'reservedFor', 'team', 'tenant', 'ownedBy'):
            val = record.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
        return 'Free'

    # ------------------------------------------------------------------
    # Public API — ports (connections)  (matches IxOSDriver interface)
    # ------------------------------------------------------------------

    def get_ports(self) -> DriverResult:
        """Fetch all front-panel connections, mapped to the port paradigm.
        Ports are tagged with panel_type: 'front' for management node
        physical ports, 'back' for compute node internal connections.
        When /introspection/connections is empty (TREX/HTREX), fall back to
        logicalports, frontpanel, or node-based placeholders."""
        conns_result = self._get('/introspection/connections')
        if not conns_result.success:
            return conns_result
        raw = conns_result.data if isinstance(conns_result.data, list) else []

        # Determine mgmt node names to tag panel_type
        mgmt_node_names = set()
        nodes_result = self._get('/introspection/nodes')
        if nodes_result.success and isinstance(nodes_result.data, list):
            for n in nodes_result.data:
                if n.get('role', '') == 'merlin':
                    mgmt_node_names.add(n.get('name', ''))

        # TREX/HTREX: connections API often empty; try logicalports, frontpanel, nodes
        if not raw:
            raw = self._fallback_ports_from_logical_or_frontpanel_or_nodes(
                mgmt_node_names, nodes_result.data if nodes_result.success else []
            )
            if raw:
                logger.info(
                    'KCOS %s: using %d fallback ports (connections empty)',
                    self.ip, len(raw),
                )

        ports = []
        for idx, c in enumerate(raw):
            link_raw = c.get('link', 'DOWN')
            link_state, led_color = self._normalize_link(link_raw)

            # Derive a port number from the 'to' field (e.g. "15.0" -> 0, "23.2" -> 2)
            to_field = c.get('to', '')
            try:
                port_num = int(to_field.split('.')[-1]) if '.' in to_field else idx
            except (ValueError, IndexError):
                port_num = idx

            try:
                slot = int(c.get('slot', 0))
            except (ValueError, TypeError):
                slot = 0

            speed_raw = c.get('speed', '')
            speed_display = self._format_speed(speed_raw)

            node_name = c.get('name', '')
            # Front panel = management node ports; back panel = compute node ports
            panel_type = 'front' if node_name in mgmt_node_names else 'back'

            ports.append({
                'id': c.get('tepid', f'port-{idx}'),
                'card_number': slot,
                'port_number': port_num,
                'owner': self._extract_kcos_ownership(c),
                'link_state': link_state,
                'link_state_raw': link_raw,
                'led_color': led_color,
                'speed': speed_display,
                'phy_mode': '',
                'transmit_state': '',
                'transceiver_model': c.get('vendorPartNumber', ''),
                'transceiver_mfg': c.get('vendor', ''),
                'type': c.get('from', ''),
                # KCOS-specific
                'tepid': c.get('tepid', ''),
                'from_interface': c.get('from', ''),
                'to_switch_port': to_field,
                'node_name': node_name,
                'auto_negotiation': c.get('autoNegotiation', ''),
                'fec_active': c.get('fecActive', []),
                'fec_configured': c.get('fecConfigured', []),
                'transceiver_serial': c.get('vendorSerialNumber', ''),
                'panel_type': panel_type,
            })

        ports.sort(key=lambda p: (p['card_number'], p['port_number']))
        return DriverResult(success=True, data=ports)

    # ------------------------------------------------------------------
    # Public API — health  (matches IxOSDriver interface)
    # ------------------------------------------------------------------

    def get_health(self) -> DriverResult:
        """Derive health from node statuses (KCOS has no perfcounters)."""
        nodes_result = self._get('/introspection/nodes')
        nodes = nodes_result.data if nodes_result.success and isinstance(nodes_result.data, list) else []
        compute = [n for n in nodes if n.get('role', '') != 'merlin']
        ready = sum(1 for n in compute if n.get('status') == 'Ready')
        total = len(compute)

        return DriverResult(success=True, data={
            'cpu_utilization': 0,   # not available via KCOS API
            'memory_used': 0,
            'memory_total': 0,
            'nodes_ready': ready,
            'nodes_total': total,
            'cluster_health': 'healthy' if ready == total else 'degraded',
        })

    # ------------------------------------------------------------------
    # Public API — sensors  (matches IxOSDriver interface)
    # ------------------------------------------------------------------

    def get_sensors(self) -> DriverResult:
        """KCOS does not expose sensor data via REST API."""
        return DriverResult(success=True, data=[])

    # ------------------------------------------------------------------
    # Public API — licensing (Helm releases as proxy)
    # ------------------------------------------------------------------

    def get_licenses(self) -> DriverResult:
        """Return Helm releases as a licensing/deployment overview."""
        result = self._get('/deployment/helm/cluster/releases')
        if not result.success:
            return DriverResult(success=True, data=[])
        raw = result.data if isinstance(result.data, list) else []
        releases = []
        for r in raw:
            releases.append({
                'name': r.get('name', ''),
                'namespace': r.get('namespace', ''),
                'first_deployed': r.get('firstDeployed', ''),
                'last_deployed': r.get('lastDeployed', ''),
            })
        return DriverResult(success=True, data=releases)

    # ------------------------------------------------------------------
    # Public API — port statistics
    # ------------------------------------------------------------------

    def get_port_stats(self) -> DriverResult:
        """KCOS port stats via netif-diagnostics aggregated NIC info."""
        agg = self.get_aggregated_nic_info()
        if not agg.success:
            return DriverResult(success=True, data=[])
        raw = agg.data
        rows: list = []
        if isinstance(raw, list):
            rows = raw
        elif isinstance(raw, dict):
            rows = (
                raw.get('interfaces')
                or raw.get('netIfs')
                or raw.get('items')
                or raw.get('data')
                or []
            )
            if not rows and 'nodes' in raw and isinstance(raw['nodes'], list):
                for node in raw['nodes']:
                    if not isinstance(node, dict):
                        continue
                    node_name = node.get('nodeName') or node.get('name') or ''
                    for iface in node.get('interfaces') or node.get('netIfs') or []:
                        if isinstance(iface, dict):
                            iface = dict(iface)
                            iface.setdefault('nodeName', node_name)
                            rows.append(iface)
        stats = []
        for idx, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            slot_raw = item.get('slot') or item.get('slotNumber') or item.get('engine') or 1
            port_raw = item.get('port') or item.get('portNumber') or item.get('to') or idx + 1
            try:
                slot = int(str(slot_raw).split('.')[0])
            except (ValueError, TypeError):
                slot = 1
            try:
                port_num = int(str(port_raw).split('.')[-1])
            except (ValueError, TypeError):
                port_num = idx + 1
            driver_stats = item.get('driverStats') or item.get('driver') or item.get('stats') or item
            rx = (
                driver_stats.get('rxBitRate')
                or driver_stats.get('rxBitrate')
                or driver_stats.get('rxRate')
                or item.get('rxBitRate')
                or 0
            )
            tx = (
                driver_stats.get('txBitRate')
                or driver_stats.get('txBitrate')
                or driver_stats.get('txRate')
                or item.get('txBitRate')
                or 0
            )
            stats.append({
                'cardNumber': slot,
                'portNumber': port_num,
                'id': item.get('id') or item.get('tepid') or f'{slot}.{port_num}',
                'rxBitRate': rx,
                'txBitRate': tx,
            })
        return DriverResult(success=True, data=stats)

    # ------------------------------------------------------------------
    # Public API — services
    # ------------------------------------------------------------------

    def get_services(self) -> DriverResult:
        """Return running pods as a proxy for services."""
        result = self._get('/introspection/pods')
        if not result.success:
            return DriverResult(success=True, data=[])
        raw = result.data if isinstance(result.data, list) else []
        services = []
        for pod in raw:
            services.append({
                'name': pod.get('name', ''),
                'namespace': pod.get('namespace', ''),
                'status': pod.get('status', ''),
                'ready': pod.get('ready', ''),
                'node': pod.get('node', ''),
            })
        return DriverResult(success=True, data=services)

    # ------------------------------------------------------------------
    # Port fallback for TREX/HTREX (connections API often empty)
    # ------------------------------------------------------------------

    def _fallback_ports_from_logical_or_frontpanel_or_nodes(
        self, mgmt_node_names: set, nodes: list
    ) -> list[dict]:
        """Build connection-like dicts from logicalports, frontpanel, or nodes.
        TREX/HTREX often have empty /introspection/connections; use alternative APIs."""
        # 1. Try logicalports
        lp_result = self._get('/introspection/logicalports')
        if lp_result.success:
            lp_data = self._normalize_list_response(lp_result.data, 'logicalports')
            if isinstance(lp_data, list) and lp_data:
                return self._map_logical_or_frontpanel_to_connections(lp_data, mgmt_node_names)

        # 2. Try frontpanel
        fp_result = self._get('/introspection/debug/hardware/frontpanel')
        if fp_result.success:
            fp_data = self._normalize_list_response(fp_result.data, 'frontpanel')
            if isinstance(fp_data, list) and fp_data:
                return self._map_logical_or_frontpanel_to_connections(fp_data, mgmt_node_names)

        # 3. Build placeholder from compute nodes
        compute_nodes = [n for n in (nodes or []) if n.get('role', '') != 'merlin']
        if not compute_nodes:
            return []
        apps_result = self._get('/introspection/apps')
        apps_by_node = {}
        if apps_result.success and isinstance(apps_result.data, dict):
            for na in apps_result.data.get('nodes', []):
                apps_by_node[na.get('nodeName', '')] = na

        out = []
        for idx, node in enumerate(compute_nodes):
            name = node.get('name', '')
            app_info = apps_by_node.get(name, {})
            slot_str = app_info.get('slotNumber', '0')
            try:
                slot = int(slot_str)
            except (ValueError, TypeError):
                slot = idx + 1
            out.append({
                'name': name,
                'slot': slot,
                'to': f'{slot}.0',
                'link': 'UNKNOWN',
                'speed': '',
                'from': node.get('osImage', '') or 'NIC',
                'tepid': f'node-{name}',
            })
        return out

    def _map_logical_or_frontpanel_to_connections(
        self, items: list, mgmt_node_names: set
    ) -> list[dict]:
        """Map logicalports or frontpanel items to connection-like dicts."""
        out = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            # Flexible field extraction (API schema may vary)
            name = (item.get('nodeName') or item.get('node') or item.get('name') or
                    item.get('node_name', ''))
            slot = item.get('slot', item.get('slotNumber', item.get('engine', idx)))
            try:
                slot = int(slot)
            except (ValueError, TypeError):
                slot = idx + 1
            to_field = item.get('to', item.get('port', item.get('interface', f'{slot}.{idx}')))
            if isinstance(to_field, (int, float)):
                to_field = f'{slot}.{int(to_field)}'
            link = item.get('link', item.get('pcsLinkStatus', item.get('status', 'UNKNOWN')))
            if isinstance(link, str) and link.upper() == 'UP':
                link = 'UP'
            elif isinstance(link, str) and link.upper() == 'DOWN':
                link = 'DOWN'
            speed = str(item.get('speed', item.get('linkSpeed', '')))
            from_if = item.get('from', item.get('interface', item.get('name', '')))
            tepid = item.get('tepid', item.get('id', f'fp-{idx}'))
            owner = KCOSDriver._extract_kcos_ownership(item)
            out.append({
                'name': name,
                'slot': slot,
                'to': str(to_field),
                'link': str(link) if link else 'UNKNOWN',
                'speed': speed,
                'from': str(from_if) if from_if else 'NIC',
                'tepid': str(tepid),
                'owner': owner,
            })
        return out

    # ------------------------------------------------------------------
    # KCOS-specific — logical ports and front panel (Phase 2)
    # ------------------------------------------------------------------

    def get_logical_ports(self) -> DriverResult:
        """Fetch logical port information from /api/v2/introspection/logicalports."""
        result = self._get('/introspection/logicalports')
        if result.success:
            result = DriverResult(success=True, data=self._normalize_list_response(result.data, 'logicalports'))
        return result

    def get_front_panel_ports(self) -> DriverResult:
        """Fetch front-panel port diagnostics from debug/hardware/frontpanel."""
        result = self._get('/introspection/debug/hardware/frontpanel')
        if result.success:
            result = DriverResult(success=True, data=self._normalize_list_response(result.data, 'frontpanel'))
        return result

    @staticmethod
    def _normalize_list_response(data, *hint_keys):
        """If data is already a list, return it. If it's a dict, try known keys."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Try the hint keys first
            for key in hint_keys:
                if key in data and isinstance(data[key], list):
                    return data[key]
            # Try common wrapper keys
            for key in ('data', 'items', 'ports', 'results'):
                if key in data and isinstance(data[key], list):
                    return data[key]
            # Last resort: if there's exactly one key and it's a list, use it
            vals = list(data.values())
            if len(vals) == 1 and isinstance(vals[0], list):
                return vals[0]
        return []

    def get_pods_by_namespace(self, namespace: str) -> DriverResult:
        """Filter pods by namespace."""
        result = self._get('/introspection/pods')
        if not result.success:
            return result
        all_pods = result.data if isinstance(result.data, list) else []
        filtered = [p for p in all_pods if p.get('namespace', '') == namespace]
        return DriverResult(success=True, data=filtered)

    # ------------------------------------------------------------------
    # KCOS-specific operations — app management
    # ------------------------------------------------------------------

    def switch_app(self, node_name: str, app_id: str, force: bool = False) -> DriverResult:
        """Switch the active app on a compute node."""
        payload = {
            'nodeName': node_name,
            'appId': app_id,
        }
        if force:
            payload['force'] = True
        result = self._post('/introspection/apps/operations/switch', payload=payload, timeout=60)
        if result.success and isinstance(result.data, dict):
            # Async operation — return the operation status
            return DriverResult(success=True, data={
                'operation_id': result.data.get('id', ''),
                'state': result.data.get('state', ''),
                'message': result.data.get('message', ''),
                'url': result.data.get('url', ''),
            })
        return result

    def get_app_switch_status(self, operation_id: str) -> DriverResult:
        """Check the status of an app-switch operation."""
        return self._get(f'/introspection/apps/operations/switch/{operation_id}')

    def set_default_app(self, app_id: str) -> DriverResult:
        """Set the default app for the cluster."""
        return self._post('/introspection/apps/operations/set-default',
                          payload={'appId': app_id})

    def get_default_app(self) -> DriverResult:
        """Get the currently set default app."""
        return self._get('/introspection/apps/default')

    # ------------------------------------------------------------------
    # KCOS-specific operations — BMC / power management
    # ------------------------------------------------------------------

    def power_cycle_node(self, node_name: str) -> DriverResult:
        """Power-cycle a node via its BMC."""
        return self._post(f'/introspection/nodes/{node_name}/bmc/operations/power-cycle')

    def power_off_node(self, node_name: str) -> DriverResult:
        """Power-off a node via its BMC."""
        return self._post(f'/introspection/nodes/{node_name}/bmc/operations/power-off')

    def power_on_node(self, node_name: str) -> DriverResult:
        """Power-on a node via its BMC."""
        return self._post(f'/introspection/nodes/{node_name}/bmc/operations/power-on')

    def get_power_status(self, node_name: str) -> DriverResult:
        """Get the power status of a node."""
        return self._post(f'/introspection/nodes/{node_name}/bmc/operations/power-status')

    def restart_node(self, node_name: str) -> DriverResult:
        """Restart a node (OS-level restart, not BMC power cycle)."""
        return self._post(f'/introspection/nodes/{node_name}/operations/restart')

    # ------------------------------------------------------------------
    # LLDP via root SSH (mgmt node → compute nodes)
    # ------------------------------------------------------------------

    def get_lldp_ssh(
        self,
        bps_topology: dict | None = None,
        chassis_type: str = '',
    ) -> DriverResult:
        """LLDP neighbors on KCOS test interfaces via root SSH.

        - M8400 (``aps_m8400``): KCOS ``lldp-producer`` DaemonSet pods on compute
          nodes (``eaglefp*fo*`` front-panel interfaces). Falls back to B2B serial
          synthesis when lldpd TX/RX does not see a neighbor on loopback/DAC links.
        - APS100/M1010: lldpd on each compute node (passwordless root SSH hop from
          mgmt using internal IP or hostname).

        Enables lldpd best-effort, maps node/interface to ``slot.port`` labels the
        topology map uses. Returns list of {local_port, remote_device, remote_port,
        chassis_id, mgmt_ip, node_name, interface}.
        """
        from django.conf import settings

        from ..kcos_ssh import collect_kcos_lldp, resolve_kcos_ssh_key
        from ..topology_lldp import (
            _bps_physical_port_in_fanout,
            kcos_connection_local_port,
            synthesize_kcos_b2b_lldp_neighbors,
        )

        password = getattr(settings, 'KCOS_ROOT_SSH_PASSWORD', '') or ''
        key_path = getattr(settings, 'KCOS_ROOT_SSH_KEY', '') or ''
        user = getattr(settings, 'KCOS_ROOT_SSH_USER', 'root') or 'root'
        port = int(getattr(settings, 'KCOS_ROOT_SSH_PORT', 9022) or 9022)
        resolved_key = resolve_kcos_ssh_key(key_path) if key_path else ''
        if not password and not resolved_key:
            return DriverResult(error='KCOS root SSH not configured (KCOS_ROOT_SSH_KEY)')

        nodes_result = self._get('/introspection/nodes')
        all_nodes = nodes_result.data if nodes_result.success and isinstance(nodes_result.data, list) else []
        merlin_node = next(
            (n for n in all_nodes if isinstance(n, dict) and n.get('role', '') == 'merlin'),
            None,
        )
        merlin_name = (merlin_node or {}).get('name', '').strip()
        is_m8400 = (chassis_type or '').strip() == 'aps_m8400'
        compute_nodes = [] if is_m8400 else [
            {'name': n.get('name', ''), 'internal_ip': n.get('internalIP', '')}
            for n in all_nodes
            if isinstance(n, dict) and n.get('role', '') != 'merlin'
        ]
        if not compute_nodes and not merlin_node:
            return DriverResult(success=True, data=[])

        if bps_topology is None and merlin_node and is_m8400:
            try:
                from .bps import BPSDriver
                bps_drv = BPSDriver(self.ip, self.username, self.password)
                bps_res = bps_drv.get_topology()
                if bps_res.success and isinstance(bps_res.data, dict):
                    bps_topology = bps_res.data
            except Exception as exc:
                logger.debug('KCOS LLDP: BPS topology for fanout filter unavailable: %s', exc)

        # Map (node, interface) → fabric port label (slot.port) used in the topology.
        iface_label: dict[tuple[str, str], str] = {}
        ports_res = self.get_ports()
        port_rows = ports_res.data if ports_res.success and isinstance(ports_res.data, list) else []
        for p in port_rows:
            if not isinstance(p, dict):
                continue
            node_name = (p.get('node_name') or '').strip()
            iface = (p.get('from_interface') or '').strip()
            label = kcos_connection_local_port(p) or f"{p.get('card_number', 0)}.{p.get('port_number', 0)}"
            if node_name and iface:
                iface_label[(node_name, iface)] = label

        # Fanout-aware LLDP enable: M8400 QDD only when broken out; APS100 all NICs.
        fanout_phys_ids: set[int] = set()
        if is_m8400 and bps_topology:
            for slot in (bps_topology or {}).get('slots', []):
                for pp in slot.get('physical_ports', []):
                    if _bps_physical_port_in_fanout(pp):
                        try:
                            fanout_phys_ids.add(int(pp.get('id', -1)))
                        except (TypeError, ValueError):
                            pass

        node_interfaces: dict[str, list[str]] = {}
        if is_m8400:
            # Front-panel fanout legs live on compute nodes (producer pods), not mgmt.
            for p in port_rows:
                if not isinstance(p, dict):
                    continue
                node_name = (p.get('node_name') or '').strip()
                iface = (p.get('from_interface') or '').strip()
                if not node_name or not iface.startswith('eaglefp'):
                    continue
                m = re.match(r'^eaglefp(\d+)(?:fo(\d+))?$', iface)
                if m and fanout_phys_ids:
                    phys_id = int(m.group(1)) + 1
                    if phys_id not in fanout_phys_ids:
                        continue
                node_interfaces.setdefault(node_name, [])
                if iface not in node_interfaces[node_name]:
                    node_interfaces[node_name].append(iface)
            # BPS lane labels when connections API lacks a row for a broken-out leg.
            for slot in (bps_topology or {}).get('slots', []):
                for pp in slot.get('physical_ports', []):
                    try:
                        phys_id = int(pp.get('id', -1))
                    except (TypeError, ValueError):
                        continue
                    if fanout_phys_ids and phys_id not in fanout_phys_ids:
                        continue
                    base_iface = f'eaglefp{phys_id - 1}'
                    lane_ids = [(lane.get('id') or '').strip() for lane in pp.get('lanes', [])]
                    for lane_id in lane_ids:
                        if not lane_id:
                            continue
                        parts = lane_id.split('.')
                        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                            fo_iface = f'{base_iface}fo{parts[1]}'
                            for node_name in node_interfaces:
                                iface_label.setdefault((node_name, fo_iface), lane_id)
        else:
            for p in port_rows:
                node_name = (p.get('node_name') or '').strip()
                iface = (p.get('from_interface') or '').strip()
                if not node_name or not iface:
                    continue
                node_interfaces.setdefault(node_name, [])
                if iface not in node_interfaces[node_name]:
                    node_interfaces[node_name].append(iface)

        per_node = collect_kcos_lldp(
            [unbracket_host(self.ip)],
            compute_nodes,
            merlin_node={'name': merlin_name} if merlin_name else None,
            node_interfaces=node_interfaces,
            probe_merlin=False,
            probe_compute=not is_m8400,
            probe_producer_pods=is_m8400,
            merlin_k8s_node='mgmt',
            merlin_use_k8s_lldpd=False,
            username=user,
            password=password,
            key_path=key_path,
            port=port,
        )

        def _lane_for_eaglefp_iface(iface: str) -> str:
            m = re.match(r'^eaglefp(\d+)(?:fo(\d+))?$', (iface or '').strip())
            if not m:
                return ''
            phys_id = int(m.group(1)) + 1
            if fanout_phys_ids and phys_id not in fanout_phys_ids:
                return ''
            lane_suffix = m.group(2)
            for slot in (bps_topology or {}).get('slots', []):
                for pp in slot.get('physical_ports', []):
                    try:
                        pp_id = int(pp.get('id', -1))
                    except (TypeError, ValueError):
                        continue
                    if pp_id != phys_id:
                        continue
                    lanes = pp.get('lanes', [])
                    if lane_suffix is not None:
                        want = f'{phys_id}.{lane_suffix}'
                        for lane in lanes:
                            if (lane.get('id') or '').strip() == want:
                                return want
                        return want
                    if lanes:
                        return (lanes[-1].get('id') or '').strip()
            return f'{phys_id}.{lane_suffix or 0}'

        neighbors: list[dict] = []
        for node_name, rows in per_node.items():
            for row in rows:
                iface = row.get('interface', '')
                local_port = iface_label.get((node_name, iface))
                if not local_port and is_m8400:
                    local_port = _lane_for_eaglefp_iface(iface)
                if not local_port:
                    local_port = f'{node_name}:{iface}'
                neighbors.append({
                    'local_port': local_port,
                    'remote_device': row.get('remote_device', ''),
                    'remote_port': row.get('remote_port', ''),
                    'chassis_id': row.get('chassis_id', ''),
                    'mgmt_ip': row.get('mgmt_ip', ''),
                    'node_name': node_name,
                    'interface': iface,
                })

        # B2B loopback/DAC on the same chassis: lldpd often sees nothing on
        # eaglefp*fo* even when the link is UP — infer peers from serial pairs.
        hostname = ''
        hn_res = self.get_hostname()
        if hn_res.success and isinstance(hn_res.data, dict):
            hostname = (hn_res.data.get('name') or '').strip()
        b2b_rows = synthesize_kcos_b2b_lldp_neighbors(
            port_rows,
            hostname=hostname,
            mgmt_ip=unbracket_host(self.ip),
        )
        locals_with_real_lldp = {
            n.get('local_port')
            for n in neighbors
            if (n.get('remote_device') or n.get('remote_port'))
        }
        for row in b2b_rows:
            if row.get('local_port') in locals_with_real_lldp:
                continue
            neighbors.append(row)

        return DriverResult(success=True, data=neighbors)

    # ------------------------------------------------------------------
    # LLDP via root SSH (mgmt node → compute nodes)
    # ------------------------------------------------------------------

    def get_lldp_ssh(
        self,
        bps_topology: dict | None = None,
        chassis_type: str = '',
    ) -> DriverResult:
        """LLDP neighbors on KCOS test interfaces via root SSH.

        - M8400 (``aps_m8400``): KCOS ``lldp-producer`` DaemonSet pods on compute
          nodes (``eaglefp*fo*`` front-panel interfaces). Falls back to B2B serial
          synthesis when lldpd TX/RX does not see a neighbor on loopback/DAC links.
        - APS100/M1010: lldpd on each compute node (passwordless root SSH hop from
          mgmt using internal IP or hostname).

        Enables lldpd best-effort, maps node/interface to ``slot.port`` labels the
        topology map uses. Returns list of {local_port, remote_device, remote_port,
        chassis_id, mgmt_ip, node_name, interface}.
        """
        from django.conf import settings

        from ..kcos_ssh import collect_kcos_lldp, resolve_kcos_ssh_key
        from ..topology_lldp import (
            _bps_physical_port_in_fanout,
            kcos_connection_local_port,
            synthesize_kcos_b2b_lldp_neighbors,
        )

        password = getattr(settings, 'KCOS_ROOT_SSH_PASSWORD', '') or ''
        key_path = getattr(settings, 'KCOS_ROOT_SSH_KEY', '') or ''
        user = getattr(settings, 'KCOS_ROOT_SSH_USER', 'root') or 'root'
        port = int(getattr(settings, 'KCOS_ROOT_SSH_PORT', 9022) or 9022)
        resolved_key = resolve_kcos_ssh_key(key_path) if key_path else ''
        if not password and not resolved_key:
            return DriverResult(error='KCOS root SSH not configured (KCOS_ROOT_SSH_KEY)')

        nodes_result = self._get('/introspection/nodes')
        all_nodes = nodes_result.data if nodes_result.success and isinstance(nodes_result.data, list) else []
        merlin_node = next(
            (n for n in all_nodes if isinstance(n, dict) and n.get('role', '') == 'merlin'),
            None,
        )
        merlin_name = (merlin_node or {}).get('name', '').strip()
        is_m8400 = (chassis_type or '').strip() == 'aps_m8400'
        compute_nodes = [] if is_m8400 else [
            {'name': n.get('name', ''), 'internal_ip': n.get('internalIP', '')}
            for n in all_nodes
            if isinstance(n, dict) and n.get('role', '') != 'merlin'
        ]
        if not compute_nodes and not merlin_node:
            return DriverResult(success=True, data=[])

        if bps_topology is None and merlin_node and is_m8400:
            try:
                from .bps import BPSDriver
                bps_drv = BPSDriver(self.ip, self.username, self.password)
                bps_res = bps_drv.get_topology()
                if bps_res.success and isinstance(bps_res.data, dict):
                    bps_topology = bps_res.data
            except Exception as exc:
                logger.debug('KCOS LLDP: BPS topology for fanout filter unavailable: %s', exc)

        # Map (node, interface) → fabric port label (slot.port) used in the topology.
        iface_label: dict[tuple[str, str], str] = {}
        ports_res = self.get_ports()
        port_rows = ports_res.data if ports_res.success and isinstance(ports_res.data, list) else []
        for p in port_rows:
            if not isinstance(p, dict):
                continue
            node_name = (p.get('node_name') or '').strip()
            iface = (p.get('from_interface') or '').strip()
            label = kcos_connection_local_port(p) or f"{p.get('card_number', 0)}.{p.get('port_number', 0)}"
            if node_name and iface:
                iface_label[(node_name, iface)] = label

        # Fanout-aware LLDP enable: M8400 QDD only when broken out; APS100 all NICs.
        fanout_phys_ids: set[int] = set()
        if is_m8400 and bps_topology:
            for slot in (bps_topology or {}).get('slots', []):
                for pp in slot.get('physical_ports', []):
                    if _bps_physical_port_in_fanout(pp):
                        try:
                            fanout_phys_ids.add(int(pp.get('id', -1)))
                        except (TypeError, ValueError):
                            pass

        node_interfaces: dict[str, list[str]] = {}
        if is_m8400:
            # Front-panel fanout legs live on compute nodes (producer pods), not mgmt.
            for p in port_rows:
                if not isinstance(p, dict):
                    continue
                node_name = (p.get('node_name') or '').strip()
                iface = (p.get('from_interface') or '').strip()
                if not node_name or not iface.startswith('eaglefp'):
                    continue
                m = re.match(r'^eaglefp(\d+)(?:fo(\d+))?$', iface)
                if m and fanout_phys_ids:
                    phys_id = int(m.group(1)) + 1
                    if phys_id not in fanout_phys_ids:
                        continue
                node_interfaces.setdefault(node_name, [])
                if iface not in node_interfaces[node_name]:
                    node_interfaces[node_name].append(iface)
            # BPS lane labels when connections API lacks a row for a broken-out leg.
            for slot in (bps_topology or {}).get('slots', []):
                for pp in slot.get('physical_ports', []):
                    try:
                        phys_id = int(pp.get('id', -1))
                    except (TypeError, ValueError):
                        continue
                    if fanout_phys_ids and phys_id not in fanout_phys_ids:
                        continue
                    base_iface = f'eaglefp{phys_id - 1}'
                    lane_ids = [(lane.get('id') or '').strip() for lane in pp.get('lanes', [])]
                    for lane_id in lane_ids:
                        if not lane_id:
                            continue
                        parts = lane_id.split('.')
                        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                            fo_iface = f'{base_iface}fo{parts[1]}'
                            for node_name in node_interfaces:
                                iface_label.setdefault((node_name, fo_iface), lane_id)
        else:
            for p in port_rows:
                node_name = (p.get('node_name') or '').strip()
                iface = (p.get('from_interface') or '').strip()
                if not node_name or not iface:
                    continue
                node_interfaces.setdefault(node_name, [])
                if iface not in node_interfaces[node_name]:
                    node_interfaces[node_name].append(iface)

        per_node = collect_kcos_lldp(
            [unbracket_host(self.ip)],
            compute_nodes,
            merlin_node={'name': merlin_name} if merlin_name else None,
            node_interfaces=node_interfaces,
            probe_merlin=False,
            probe_compute=not is_m8400,
            probe_producer_pods=is_m8400,
            merlin_k8s_node='mgmt',
            merlin_use_k8s_lldpd=False,
            username=user,
            password=password,
            key_path=key_path,
            port=port,
        )

        def _lane_for_eaglefp_iface(iface: str) -> str:
            m = re.match(r'^eaglefp(\d+)(?:fo(\d+))?$', (iface or '').strip())
            if not m:
                return ''
            phys_id = int(m.group(1)) + 1
            if fanout_phys_ids and phys_id not in fanout_phys_ids:
                return ''
            lane_suffix = m.group(2)
            for slot in (bps_topology or {}).get('slots', []):
                for pp in slot.get('physical_ports', []):
                    try:
                        pp_id = int(pp.get('id', -1))
                    except (TypeError, ValueError):
                        continue
                    if pp_id != phys_id:
                        continue
                    lanes = pp.get('lanes', [])
                    if lane_suffix is not None:
                        want = f'{phys_id}.{lane_suffix}'
                        for lane in lanes:
                            if (lane.get('id') or '').strip() == want:
                                return want
                        return want
                    if lanes:
                        return (lanes[-1].get('id') or '').strip()
            return f'{phys_id}.{lane_suffix or 0}'

        neighbors: list[dict] = []
        for node_name, rows in per_node.items():
            for row in rows:
                iface = row.get('interface', '')
                local_port = iface_label.get((node_name, iface))
                if not local_port and is_m8400:
                    local_port = _lane_for_eaglefp_iface(iface)
                if not local_port:
                    local_port = f'{node_name}:{iface}'
                neighbors.append({
                    'local_port': local_port,
                    'remote_device': row.get('remote_device', ''),
                    'remote_port': row.get('remote_port', ''),
                    'chassis_id': row.get('chassis_id', ''),
                    'mgmt_ip': row.get('mgmt_ip', ''),
                    'node_name': node_name,
                    'interface': iface,
                })

        # B2B loopback/DAC on the same chassis: lldpd often sees nothing on
        # eaglefp*fo* even when the link is UP — infer peers from serial pairs.
        hostname = ''
        hn_res = self.get_hostname()
        if hn_res.success and isinstance(hn_res.data, dict):
            hostname = (hn_res.data.get('name') or '').strip()
        b2b_rows = synthesize_kcos_b2b_lldp_neighbors(
            port_rows,
            hostname=hostname,
            mgmt_ip=unbracket_host(self.ip),
        )
        locals_with_real_lldp = {
            n.get('local_port')
            for n in neighbors
            if (n.get('remote_device') or n.get('remote_port'))
        }
        for row in b2b_rows:
            if row.get('local_port') in locals_with_real_lldp:
                continue
            neighbors.append(row)

        return DriverResult(success=True, data=neighbors)

    # ------------------------------------------------------------------
    # KCOS-specific — NIC diagnostics
    # ------------------------------------------------------------------

    def get_aggregated_nic_info(self) -> DriverResult:
        """Fetch aggregated NIC info (config + driver + transceiver) for all
        nodes and interfaces using regex match."""
        payload = {
            'node': '.*',
            'netIf': '.*',
            'nodeType': 'REGEXP_TYPE',
            'netIfType': 'REGEXP_TYPE',
        }
        return self._post('/netif-diagnostics/operations/retrieve-aggregated-info',
                          payload=payload, timeout=60)

    def get_node_interfaces(self, node_name: str) -> DriverResult:
        """List front-panel interfaces on a given node."""
        return self._get(f'/netif-diagnostics/nodes/{node_name}/interfaces')

    def get_interface_detail(self, node_name: str, interface_name: str) -> DriverResult:
        """Fetch full detail (config + driver + transceiver) for one interface."""
        return self._get(
            f'/netif-diagnostics/nodes/{node_name}/interfaces/{interface_name}')

    # ------------------------------------------------------------------
    # KCOS-specific — firmware
    # ------------------------------------------------------------------

    def get_firmware_components(self) -> DriverResult:
        """Return firmware components available for upgrade."""
        return self._get('/firmware-controller/components')

    def upgrade_firmware(self, components: list) -> DriverResult:
        """Initiate a firmware upgrade for specified components."""
        return self._post('/firmware-controller/operations/upgrade',
                          payload=components, timeout=120)

    def get_firmware_upgrade_status(self) -> DriverResult:
        """Check the status of an ongoing firmware upgrade."""
        return self._get('/firmware-controller/operations/upgrade/status')

    # ------------------------------------------------------------------
    # KCOS-specific — deployment / upgrade
    # ------------------------------------------------------------------

    _DEPLOY_BASE = '/deployment/helm'

    def get_available_updates(self, chart_name: str) -> DriverResult:
        """Fetch available online versions for a given chart.

        GET /deployment/helm/repo/charts/{chart_name}/all-deployments
        Note: response can be large (1000+ entries), so use a longer timeout.
        """
        return self._get(f'{self._DEPLOY_BASE}/repo/charts/{chart_name}/all-deployments',
                         timeout=30)

    def get_staging_status(self) -> DriverResult:
        """Get the current staging area status.

        GET /deployment/helm/cluster/staging
        Returns: {isResolved, charts[], dependencyCharts[], images[],
                  missingRequires[], brokenReleases[], existingRejects}
        """
        return self._get(f'{self._DEPLOY_BASE}/cluster/staging')

    def stage_build(self, package_url: str) -> DriverResult:
        """Stage a build from a URL (online repo URL or hosted offline package).

        POST /deployment/helm/cluster/staging/operations/add
        Payload: {"packages": [{"path": "<url>"}]}
        Returns: {url: "<staging operation URL to poll>", ...}
        """
        return self._post(
            f'{self._DEPLOY_BASE}/cluster/staging/operations/add',
            payload={'packages': [{'path': package_url}]},
            timeout=60,
        )

    def get_staging_operation_progress(self, operation_url: str) -> DriverResult:
        """Poll the progress of a staging operation.

        GET <operation_url>  (absolute path returned by stage_build)
        Returns: {state: "SUCCESS"|"ERROR"|"IN_PROGRESS", progress: 0-100, ...}
        """
        # operation_url is an absolute API path like /api/v2/deployment/helm/...
        # Strip the /api/v2 prefix if present to make it relative to base
        path = operation_url
        if path.startswith('/api/v2'):
            path = path[len('/api/v2'):]
        return self._get(path)

    def deploy_staged(self) -> DriverResult:
        """Deploy the currently staged builds.

        POST /deployment/helm/cluster/staging/operations/deploy
        Payload: {}
        """
        return self._post(
            f'{self._DEPLOY_BASE}/cluster/staging/operations/deploy',
            payload={},
            timeout=60,
        )

    def get_deploy_status(self) -> DriverResult:
        """Poll the deployment progress.

        GET /deployment/helm/cluster/staging/operations/deploy/status
        Returns: {state, progress, message, result: {releases[], error: {summary, error}}, url}
        """
        return self._get(f'{self._DEPLOY_BASE}/cluster/staging/operations/deploy/status')

    def get_deploy_messages(self) -> DriverResult:
        """Get human-readable deployment progress messages.

        GET /deployment/helm/cluster/staging/deploy/display-messages
        """
        return self._get(f'{self._DEPLOY_BASE}/cluster/staging/deploy/display-messages')

    # ------------------------------------------------------------------
    # KCOS-specific — snapshots
    # ------------------------------------------------------------------

    def get_snapshots(self) -> DriverResult:
        """List all snapshots."""
        return self._get('/vital/snapshots')

    def create_snapshot(self, label: str) -> DriverResult:
        """Create a new snapshot."""
        return self._post('/vital/snapshots', payload={'label': label}, timeout=120)

    def restore_snapshot(self, name: str) -> DriverResult:
        """Restore a snapshot by name."""
        return self._post(f'/vital/snapshots/{name}/operations/restore', timeout=120)

    def delete_snapshot(self, name: str) -> DriverResult:
        """Delete a snapshot by name."""
        if not self._ensure_auth():
            return DriverResult(error='Authentication failed')
        session = _get_session(self.ip)
        url = f'{self._base_url}/vital/snapshots/{name}'
        try:
            resp = session.delete(url, headers=self._headers(),
                                  verify=False, timeout=30)
            if resp.status_code == 401:
                self._token = None
                if self._authenticate():
                    resp = session.delete(url, headers=self._headers(),
                                          verify=False, timeout=30)
            if resp.status_code in (200, 204):
                return DriverResult(success=True, data={'deleted': name})
            return DriverResult(error=f'HTTP {resp.status_code}')
        except Exception as e:
            return DriverResult(error=str(e))

    def get_snapshot_status(self, operation_id: str) -> DriverResult:
        """Check snapshot operation status."""
        return self._get(f'/vital/snapshots/status/{operation_id}')

    # ------------------------------------------------------------------
    # KCOS-specific — cluster management
    # ------------------------------------------------------------------

    def get_hostname(self) -> DriverResult:
        """Get the chassis hostname."""
        return self._get('/vital/hostname')

    def get_system_time(self) -> DriverResult:
        """Get current system time."""
        return self._get('/vital/time')

    def get_firewall_status(self) -> DriverResult:
        """Get firewall status and rules."""
        return self._get('/firewall/status')

    def get_pods(self) -> DriverResult:
        """Get all pods in the cluster."""
        return self._get('/introspection/pods')

    def get_hosts(self) -> DriverResult:
        """Get all DHCP hosts known to the cluster."""
        return self._get('/introspection/hosts')

    def reboot_chassis(self) -> DriverResult:
        """Reboot the entire KCOS cluster."""
        return self._post('/vital/control', payload={'name': 'reboot'})

    def get_node_inventory(self) -> DriverResult:
        """Fetch comprehensive node inventory: nodes + BMCs + hosts + firmware.

        Merges data from four endpoints to build a complete hardware inventory
        per node, including BMC IP, serial number, firmware details (BIOS, BMC,
        NIC, SSD), and DHCP host info.

        Returns a list of dicts, one per node (mgmt + all compute nodes).
        """
        from concurrent.futures import ThreadPoolExecutor

        futures = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures['nodes'] = pool.submit(self._get, '/introspection/nodes')
            futures['bmcs'] = pool.submit(self._get, '/introspection/bmcs')
            futures['hosts'] = pool.submit(self._get, '/introspection/hosts')
            futures['firmware'] = pool.submit(self._get, '/firmware-controller/components')

        nodes_r = futures['nodes'].result()
        bmcs_r = futures['bmcs'].result()
        hosts_r = futures['hosts'].result()
        fw_r = futures['firmware'].result()

        nodes = nodes_r.data if nodes_r.success and isinstance(nodes_r.data, list) else []
        bmcs = bmcs_r.data if bmcs_r.success and isinstance(bmcs_r.data, list) else []
        hosts = hosts_r.data if hosts_r.success and isinstance(hosts_r.data, list) else []
        fw_list = fw_r.data if fw_r.success and isinstance(fw_r.data, list) else []

        if not nodes:
            return DriverResult(success=True, data=[])

        from ..keysight_aps_standalone import (
            is_mgmt_kcos_role,
            is_standalone_kcos_api_nodes,
            node_operating_mode,
            primary_bmc_ip,
        )

        standalone_topo = is_standalone_kcos_api_nodes(nodes)
        chassis_hostname = ''
        hn_result = self.get_hostname()
        if hn_result.success and isinstance(hn_result.data, dict):
            chassis_hostname = hn_result.data.get('name', '') or ''

        # Index BMCs by node name
        bmc_by_node: dict[str, dict] = {}
        for bmc in bmcs:
            nname = bmc.get('data', {}).get('nodeName', '')
            if nname:
                bmc_by_node[nname] = bmc

        # Index hosts by hostname (lowercase for matching)
        host_by_name: dict[str, dict] = {}
        for h in hosts:
            hn = (h.get('hostname') or '').lower()
            host_by_name[hn] = h

        # Index firmware by node name
        fw_by_node: dict[str, list] = {}
        for fw_entry in fw_list:
            nn = fw_entry.get('node-name', '')
            fw_by_node[nn] = fw_entry.get('firmwares', [])

        inventory = []
        for node in nodes:
            name = node.get('name', '')
            kcos_role = node.get('role', '')
            is_mgmt = is_mgmt_kcos_role(kcos_role)
            is_standalone_node = standalone_topo

            # BMC info
            bmc_info = bmc_by_node.get(name, {})
            bmc_data = bmc_info.get('data', {})
            bmc_name = bmc_info.get('bmcName', '')
            serial = bmc_data.get('serialNumber', '')
            bmc_hostname = bmc_data.get('hostname', '')
            power = bmc_info.get('power', 'unknown')

            # Try to find BMC IP from hosts (BMC hostname ends with '-bmc')
            bmc_ip = ''
            # Try multiple hostname patterns
            for candidate in [
                bmc_hostname.lower(),
                f'{name}-bmc'.lower(),
                f'{chassis_hostname}-bmc'.lower() if chassis_hostname else '',
                serial.lower() + '-bmc' if serial else '',
            ]:
                if candidate and candidate in host_by_name:
                    bmc_ip = host_by_name[candidate].get('IP', '')
                    break
            if not bmc_ip:
                bmc_ip = primary_bmc_ip(node)
            if not bmc_hostname and chassis_hostname:
                bmc_hostname = f'{chassis_hostname}-bmc'
            # For mgmt node, look for "<hostname>-bmc" pattern
            if not bmc_ip and is_mgmt:
                # mgmt BMC often named like "merpro2b-bmc"
                for hn, hdata in host_by_name.items():
                    if hn.endswith('-bmc') and hdata.get('vendorClass', '') == '':
                        bmc_ip = hdata.get('IP', '')
                        break

            # Node IP from hosts (for compute nodes)
            node_ip = node.get('internalIP', '')
            # Try to find external IP from hosts
            node_hostname_lower = name.lower()
            host_info = host_by_name.get(node_hostname_lower, {})
            dhcp_ip = host_info.get('IP', '')

            # Firmware components
            firmwares = fw_by_node.get(name, [])
            bios_ver = ''
            bmc_fw_ver = ''
            bmc_fw_name = ''
            nics = []
            ssds = []
            other_fw = []
            for fw in firmwares:
                fw_type = fw.get('type', '')
                fw_name = fw.get('name', '')
                fw_version = fw.get('version', '')
                fw_update = fw.get('update-available', 'false')
                fw_item = {
                    'type': fw_type,
                    'name': fw_name,
                    'version': fw_version,
                    'update_available': fw_update == 'true',
                }
                if fw_type == 'BIOS':
                    bios_ver = fw_version
                    other_fw.append(fw_item)
                elif fw_type == 'BMC':
                    bmc_fw_ver = fw_version
                    bmc_fw_name = fw_name
                    other_fw.append(fw_item)
                elif fw_type.startswith('NIC'):
                    nics.append(fw_item)
                elif fw_type.startswith('SSD'):
                    ssds.append(fw_item)
                else:
                    other_fw.append(fw_item)

            op_mode = node_operating_mode(kcos_role, name, is_standalone_node)
            if is_standalone_node:
                display_role = 'Standalone'
            elif is_mgmt:
                display_role = 'Management'
            else:
                display_role = 'Compute'

            entry = {
                'node_name': name,
                'role': display_role,
                'kcos_role': kcos_role,
                'operating_mode': op_mode,
                'status': node.get('status', ''),
                'internal_ip': node_ip,
                'dhcp_ip': dhcp_ip,
                'os_image': node.get('osImage', ''),
                'kernel_version': node.get('kernelVersion', ''),
                'k8s_version': node.get('version', ''),
                'container_runtime': node.get('containerRuntime', ''),
                # BMC
                'bmc_name': bmc_name,
                'bmc_hostname': bmc_hostname,
                'bmc_ip': bmc_ip,
                'bmc_power': power,
                'serial_number': serial,
                # Firmware
                'bios_version': bios_ver,
                'bmc_firmware': bmc_fw_ver,
                'bmc_firmware_name': bmc_fw_name,
                'nics': nics,
                'ssds': ssds,
                'firmware_components': other_fw,
                'firmware_count': len(firmwares),
            }
            inventory.append(entry)

        # Sort: mgmt/standalone first, then compute nodes by name
        inventory.sort(key=lambda x: (0 if x['role'] in ('Management', 'Standalone') else 1, x['node_name']))
        return DriverResult(success=True, data=inventory)

    # ------------------------------------------------------------------
    # Stub operations to match IxOS interface (no-ops on KCOS)
    # ------------------------------------------------------------------

    def take_ownership(self, port_id) -> DriverResult:
        """KCOS has no port ownership — no-op."""
        return DriverResult(success=True, data={'message': 'Not applicable for KCOS'})

    def release_ownership(self, port_id) -> DriverResult:
        """KCOS has no port ownership — no-op."""
        return DriverResult(success=True, data={'message': 'Not applicable for KCOS'})

    def reboot_port(self, port_id) -> DriverResult:
        """KCOS has no per-port reboot — no-op."""
        return DriverResult(success=True, data={'message': 'Not applicable for KCOS'})

    def reset_port(self, port_id) -> DriverResult:
        """KCOS has no per-port reset — no-op."""
        return DriverResult(success=True, data={'message': 'Not applicable for KCOS'})

    def hotswap_card(self, card_id) -> DriverResult:
        """KCOS has no hotswap — no-op."""
        return DriverResult(success=True, data={'message': 'Not applicable for KCOS'})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # IxOS-style /chassis/api/v2 REST (HTRex / T-Rex on KCOS)
    # ------------------------------------------------------------------

    def _chassis_api_base(self) -> str:
        return f'https://{self.ip}/chassis/api/v2'

    def _get_chassis_api(self, subpath: str, timeout: int = 12) -> DriverResult:
        """GET under /chassis/api/v2 (not /api/v2). Reuses KCOS bearer token."""
        path = subpath if subpath.startswith('/') else f'/{subpath}'
        if not self._ensure_auth():
            return DriverResult(error='Authentication failed')
        session = _get_session(self.ip)
        url = self._chassis_api_base() + path
        try:
            resp = session.get(url, headers=self._headers(), verify=False, timeout=timeout)
            if resp.status_code == 401:
                self._token = None
                if self._authenticate():
                    resp = session.get(url, headers=self._headers(), verify=False, timeout=timeout)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = resp.text
                return DriverResult(success=True, data=data)
            return DriverResult(error=f'HTTP {resp.status_code}')
        except Exception as e:
            return DriverResult(error=str(e))

    def _map_chassis_api_port_to_labvault(self, port: dict, idx: int) -> dict:
        link_raw = port.get('linkState', port.get('link', 'DOWN'))
        link_state, led_color = self._normalize_link(link_raw)
        card = port.get('cardNumber', port.get('slot', 0))
        port_num = port.get('portNumber', idx)
        try:
            card = int(card)
        except (TypeError, ValueError):
            card = 0
        try:
            port_num = int(port_num)
        except (TypeError, ValueError):
            port_num = idx
        speed_raw = port.get('speed', '')
        return {
            'id': port.get('id', f'{card}.{port_num}'),
            'card_number': card,
            'port_number': port_num,
            'owner': self._extract_kcos_ownership(port),
            'link_state': link_state,
            'link_state_raw': link_raw,
            'led_color': led_color,
            'speed': self._format_speed(str(speed_raw)) if speed_raw else '',
            'transceiver': port.get('transceiverModel', '') or '',
        }

    def _ports_from_chassis_rest_api(self) -> list[dict] | None:
        """Fetch ports from /chassis/api/v2; try bare /ports then /ixos/ports."""
        for subpath in ('/ports', '/ixos/ports'):
            result = self._get_chassis_api(subpath)
            if not result.success:
                continue
            raw = result.data if isinstance(result.data, list) else []
            if not raw:
                continue
            return [self._map_chassis_api_port_to_labvault(p, i) for i, p in enumerate(raw)]
        return None

    def _merge_num_ports_from_chassis_rest_api(self, cards: list[dict]) -> None:
        result = self._get_chassis_api('/cards')
        if not result.success or not isinstance(result.data, list):
            return
        by_card = {
            int(c.get('cardNumber', -1)): int(c.get('numberOfPorts', 0) or 0)
            for c in result.data
            if c.get('cardNumber') is not None
        }
        for card in cards:
            if card.get('is_mgmt_slot'):
                continue
            num = card.get('card_number')
            try:
                num = int(num)
            except (TypeError, ValueError):
                continue
            if card.get('num_ports', 0):
                continue
            if num in by_card and by_card[num] > 0:
                card['num_ports'] = by_card[num]

    @staticmethod
    def _normalize_link(raw_state) -> tuple[str, str]:
        """Normalize KCOS link state to (state, led_color).
        LED colors follow the LabVault convention:
          green  = link up
          red    = link down
          off    = unknown / no data
        """
        if raw_state is None or raw_state == '':
            return 'unknown', 'off'
        low = str(raw_state).strip().lower()
        if low == 'up':
            return 'up', 'green'
        if low == 'down':
            return 'down', 'red'
        if low in ('disabled', 'admin_down'):
            return 'disabled', 'yellow'
        return raw_state, 'off'

    @staticmethod
    def _format_speed(speed_str: str) -> str:
        """Convert raw speed string (Mbps) to human-readable form."""
        try:
            mbps = int(speed_str)
            if mbps >= 1000:
                return f'{mbps // 1000}G'
            return f'{mbps}M'
        except (ValueError, TypeError):
            return speed_str or ''
