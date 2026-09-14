"""
Keysight device driver - uses SNMP for LLDP (Keysight/Ixia chassis with LLDP support).
Keysight hardware ports often run LLDP and expose it via standard LLDP-MIB (IEEE 802.1AB).
"""
import logging
import re

from .base import BaseDriver, DriverResult

logger = logging.getLogger(__name__)

# LLDP-MIB OIDs (IEEE 802.1AB)
OID_LLDP_REM_TABLE = '1.0.8802.1.1.2.1.4.1'
OID_LLDP_REM_CHASSIS_ID = '1.0.8802.1.1.2.1.4.1.1.5'
OID_LLDP_REM_PORT_ID = '1.0.8802.1.1.2.1.4.1.1.7'
OID_LLDP_REM_SYS_NAME = '1.0.8802.1.1.2.1.4.1.1.9'
OID_LLDP_LOC_PORT_TABLE = '1.0.8802.1.1.2.1.3.7.1.1.1'  # lldpLocPortId -> port name
OID_SYS_DESCR = '1.3.6.1.2.1.1.1.0'
OID_SYS_NAME = '1.3.6.1.2.1.1.5.0'


class KeysightDriver(BaseDriver):
    """Driver for Keysight hardware that exposes LLDP via SNMP."""

    VENDOR_NAME = 'keysight'

    def __init__(self, device):
        super().__init__(device)
        self.snmp_community = getattr(device, 'snmp_community', '') or 'public'
        self.snmp_port = 161

    def _snmp_get(self, oid):
        from connect.snmp_utils import snmp_get
        saved = self.ip
        for target in self.iter_connect_targets():
            self.ip = target
            val = snmp_get(
                self.ip, self.snmp_community, oid,
                port=self.snmp_port, timeout=5,
            )
            if val and 'No Such' not in str(val):
                return val
        self.ip = saved
        return None

    def _snmp_walk(self, oid):
        from connect.snmp_utils import snmp_walk
        saved = self.ip
        for target in self.iter_connect_targets():
            self.ip = target
            rows = snmp_walk(
                self.ip, self.snmp_community, oid,
                port=self.snmp_port, timeout=10,
            )
            if rows:
                return rows
        self.ip = saved
        return None

    def probe(self) -> str:
        """Probe via SNMP sysDescr."""
        val = self._snmp_get(OID_SYS_DESCR)
        if val:
            return 'ok'
        return 'unreachable'

    def get_base_url(self, proto=None) -> str:
        return f'https://{self.ip}'

    def get_system_info(self) -> DriverResult:
        name = self._snmp_get(OID_SYS_NAME)
        descr = self._snmp_get(OID_SYS_DESCR)
        return DriverResult(success=True, data={
            'hostname': (name or '').strip() or self.ip,
            'version': '',
            'model': (descr or '')[:200] if descr else '',
            'serial_number': '',
            'uptime': None,
        })

    def get_interfaces(self) -> DriverResult:
        return DriverResult(success=True, data={'physical_data': [], 'logical_data': []})

    def get_health(self) -> DriverResult:
        return DriverResult(success=True, data={
            'cpu_utilization': 0, 'memory_used': 0, 'memory_total': 0,
        })

    def get_routes(self) -> DriverResult:
        return DriverResult(success=True, data=[])

    def get_vlans(self) -> DriverResult:
        return DriverResult(success=True, data=[])

    def get_running_config(self) -> DriverResult:
        return DriverResult(success=False, error='Not supported')

    def get_startup_config(self) -> DriverResult:
        return DriverResult(success=False, error='Not supported')

    def get_lldp_neighbors(self) -> DriverResult:
        return self.get_lldp_neighbors_detail()

    def get_lldp_neighbors_detail(self) -> DriverResult:
        """Fetch LLDP neighbors via SNMP LLDP-MIB (lldpRemTable)."""
        try:
            # Build local port num -> port name mapping from lldpLocPortId
            port_names = {}
            for suffix, val in self._snmp_walk(OID_LLDP_LOC_PORT_TABLE) or []:
                if val:
                    parts = suffix.split('.')
                    if parts:
                        try:
                            port_num = int(parts[-1])
                            port_names[port_num] = (val or '').strip() or f'Port{port_num}'
                        except (ValueError, IndexError):
                            pass

            # Build lookup dicts for chassis_id and port_id (same index suffix as sysname)
            chassis_by_suffix = {}
            port_by_suffix = {}
            for s, v in self._snmp_walk(OID_LLDP_REM_CHASSIS_ID) or []:
                if s and v:
                    chassis_by_suffix[s] = (v or '').strip()
            for s, v in self._snmp_walk(OID_LLDP_REM_PORT_ID) or []:
                if s and v:
                    port_by_suffix[s] = (v or '').strip()

            # Walk lldpRemSysName; suffix format: timeMark.portNum.remIndex (e.g. 0.1.1)
            neighbors = []
            seen = set()

            for suffix, sysname in self._snmp_walk(OID_LLDP_REM_SYS_NAME) or []:
                if not suffix:
                    continue
                parts = suffix.split('.')
                if len(parts) >= 2:
                    try:
                        port_num = int(parts[-2])
                        rem_idx = int(parts[-1])
                        key = (port_num, rem_idx)
                        if key in seen:
                            continue
                        seen.add(key)
                    except (ValueError, IndexError):
                        continue
                else:
                    continue

                local_port = port_names.get(port_num, f'Port{port_num}')
                chassis_id = chassis_by_suffix.get(suffix, '')
                port_id = port_by_suffix.get(suffix, '')

                # mgmt_ip: LLDP management address is in lldpRemManAddrTable (separate)
                mgmt_ip = ''

                remote_device = (sysname or '').strip() or port_id or chassis_id or 'unknown'

                neighbors.append({
                    'local_port': local_port,
                    'remote_device': remote_device,
                    'remote_port': port_id or '',
                    'chassis_id': chassis_id,
                    'mgmt_ip': mgmt_ip,
                })
            return DriverResult(success=True, data=neighbors)
        except Exception as e:
            logger.warning('Keysight LLDP SNMP failed for %s: %s', self.ip, e)
            return DriverResult(success=False, error=str(e))

    def enable_lldp(self) -> DriverResult:
        return DriverResult(success=False, error='LLDP enablement not supported for Keysight SNMP driver')
