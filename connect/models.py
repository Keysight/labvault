# connect/models.py
"""All ORM models for LabVault (single ``connect`` app).

Domains, in file order:

* Network devices — ``Device``, ``DeviceGroup``, ``DeviceSnapshot``, ``InterfaceSnapshot``,
  ``Alert``, ``ConfigBackup``, ``ComplianceRule`` / ``ComplianceResult``, ``SavedCommand``,
  ``TopologyLink``, ``ChassisDeviceLink``.
* Ops settings — ``ScheduledJob``, ``MaintenanceWindow`` (+ ``MaintenanceWindowDevice``),
  ``WebhookEndpoint``, ``APIToken``.
* Keysight hardware — ``KeysightChassis`` and snapshots, subnet scans, reservations,
  deployment jobs, ``KeysightBmcEndpoint``.
* Time series (``np_timeseries`` DB via ``connect.db_routers``) — ``NPResourceSample``,
  ``PortUsageSample``, ``LabMetricSample``, ``LabMetricRollup``, ``LabResourceEvent``.
* Lab Topology Designer — ``LabTopology``, ``LabTopologyNode``, ``LabTopologyLink``,
  ``TestSetupTemplate`` / ``TestSetupRun``, ``OcsPatchSnapshot``,
  ``LabTopologyFabricSnapshot``, ``TopologyAuditLog``.
* Audit / change tracking — ``AuditLog``, ``RequestLog``, ``ChangeLogEvent``,
  ``DeviceStateBaseline``.
* Preferences / runtime / CLI — ``LabvaultUserPrefs``, ``LabvaultGlobalPrefs``,
  ``RuntimeSetting``, ``CliInvocation``, ``CliJob``, ``CliAuthThrottle``.

Device and chassis credentials are stored in plain ``CharField`` columns (drivers need
the cleartext to log in). Every concrete model is auto-registered in ``connect.admin``.
"""

from django.db import models
from django.contrib.auth.models import User
from django.conf import settings
from django.utils import timezone
import json

PREFERRED_IP_VERSION_CHOICES = [
    ('ipv4', 'Prefer IPv4'),
    ('ipv6', 'Prefer IPv6'),
    ('ipv6_slaac', 'Prefer IPv6 SLAAC'),
    ('dual', 'Dual stack'),
    # Legacy — hidden in forms; migrated to ipv4 or ipv6
    ('auto', 'Auto (legacy)'),
]

# Shown in device/chassis forms (exactly four modes)
CONNECT_VIA_UI_CHOICES = [
    ('ipv4', 'Prefer IPv4'),
    ('ipv6', 'Prefer IPv6'),
    ('ipv6_slaac', 'Prefer IPv6 SLAAC'),
    ('dual', 'Dual stack'),
]

MGMT_IPV6_SOURCE_CHOICES = [
    ('', 'Not set'),
    ('dhcpv6', 'DHCPv6 (derived or manual)'),
    ('slaac', 'SLAAC (link-local / RA)'),
    ('static', 'Static IPv6'),
]


KEYSIGHT_CHASSIS_TYPE_CHOICES = [
    ('xgs2', 'XGS2'),
    ('xgs12', 'XGS12'),
    ('xg', 'XG (Legacy)'),
    ('xm', 'XM (Legacy)'),
    ('aps_m1010', 'APS-M1010'),
    ('aps_m8400', 'APS-M8400'),
    ('aps_standalone', 'APS Standalone (mgmt+CN)'),
    ('aresone', 'AresONE'),
    ('aresone_htrex', 'AresONE 1600GE HTREX'),
    ('trex', 'T-Rex'),
    ('ixvm', 'IxVM / Virtual Test Appliance'),
    ('other', 'Other'),
]

KEYSIGHT_STATUS_CHOICES = [
    ('online', 'Online'),
    ('offline', 'Offline'),
    ('auth_failed', 'Auth Failed'),
    ('unknown', 'Unknown'),
    ('maintenance', 'Maintenance'),
]

KEYSIGHT_CHASSIS_STATE_CHOICES = [
    ('ready', 'Ready'),
    ('degraded', 'Degraded'),
    ('not_connected', 'Not Connected'),
    ('chassis_error', 'Chassis Error'),
    ('unknown', 'Unknown'),
]

KEYSIGHT_OS_PLATFORM_CHOICES = [
    ('ixos', 'IxOS'),
    ('kcos', 'KCOS'),
    ('unknown', 'Unknown'),
]


KEYSIGHT_RESERVATION_STATUS_CHOICES = [
    ('active', 'Active'),
    ('upcoming', 'Upcoming'),
    ('expired', 'Expired'),
    ('cancelled', 'Cancelled'),
]


DEPLOY_JOB_TYPE_CHOICES = [
    ('online', 'Online'),
    ('offline', 'Offline'),
]

DEPLOY_PACKAGE_TYPE_CHOICES = [
    ('kcos', 'KCOS'),
    ('bps', 'BPS (BreakingPoint)'),
    ('ixload', 'IxLoad'),
    ('ixload-drv', 'IxLoad Driver Package'),
    ('cyperf', 'CyPerf Agent'),
    ('cyperf-ati', 'CyPerf ATI Update'),
    ('cyperf-ctrl', 'CyPerf Controller'),
]

DEPLOY_STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('staging', 'Staging'),
    ('staged', 'Staged'),
    ('deploying', 'Deploying'),
    ('success', 'Success'),
    ('error', 'Error'),
    ('cancelled', 'Cancelled'),
]


BMC_SOURCE_CHOICES = [
    ('chassis_derived', 'Chassis-Derived'),
    ('manual_import', 'Manual Import'),
]

BMC_OPERATING_MODE_CHOICES = [
    ('unknown', 'Unknown'),
    ('standalone_merged', 'Standalone mgmt+CN'),
    ('chassis_mgmt', 'Chassis mgmt node'),
    ('chassis_cn', 'Chassis compute node'),
]


SNMP_VERSION_CHOICES = [
    ('v1', 'SNMPv1'),
    ('v2c', 'SNMPv2c'),
    ('v3', 'SNMPv3'),
]

SNMP_DEVICE_TYPE_CHOICES = [
    ('pdu', 'PDU'),
    ('cooling', 'Cooling Unit'),
    ('server', 'Server / Host'),
    ('switch', 'Network Switch'),
    ('bmc', 'BMC / IPMI'),
    ('other', 'Other'),
]

SNMP_DEVICE_STATUS_CHOICES = [
    ('online', 'Online'),
    ('offline', 'Offline'),
    ('auth_error', 'Auth Error'),
    ('timeout', 'Timeout'),
    ('unknown', 'Unknown'),
]

SENSOR_TYPE_CHOICES = [
    ('power_w', 'Power (W)'),
    ('temperature_c', 'Temperature (°C)'),
    ('temperature_f', 'Temperature (°F)'),
    ('humidity', 'Humidity (%)'),
    ('cpu_pct', 'CPU (%)'),
    ('memory_pct', 'Memory (%)'),
    ('disk_pct', 'Disk (%)'),
    ('energy_kwh', 'Energy (kWh)'),
    ('current_a', 'Current (A)'),
    ('voltage_v', 'Voltage (V)'),
    ('fan_rpm', 'Fan Speed (RPM)'),
    ('other', 'Other'),
]

ALARM_OPERATOR_CHOICES = [
    ('gt', 'Greater Than'),
    ('lt', 'Less Than'),
    ('gte', 'Greater or Equal'),
    ('lte', 'Less or Equal'),
    ('eq', 'Equal'),
]

ALARM_SEVERITY_CHOICES = [
    ('critical', 'Critical'),
    ('warning', 'Warning'),
    ('info', 'Info'),
]


PDU_OUTLET_STATUS_CHOICES = [
    ('on', 'On'),
    ('off', 'Off'),
    ('unknown', 'Unknown'),
]


class Device(models.Model):
    """A managed network device (switch, firewall, OCS photonic switch, ...).

    ``vendor_type`` selects the driver via ``connect.drivers.get_driver``. Discovery fields
    (hostname, version, serial, status, last_seen, ...) are overwritten by
    ``views.fetch_device_data``; live interface/LLDP data is *not* stored here but in the
    shared file cache (``device_data:v1:<id>``). Address properties delegate to
    ``connect.ip_addressing`` for dual-stack resolution.
    """

    VENDOR_CHOICES = [
        ('arista', 'Arista EOS'),
        ('sonic', 'SONiC'),
        ('fortigate', 'FortiGate'),
        ('paloalto', 'Palo Alto'),
        ('keysight', 'Keysight'),
        ('ocs', 'OCS Photonic'),
    ]
    STATUS_CHOICES = [
        ('online', 'Online'),
        ('offline', 'Offline'),
        ('auth_failed', 'Auth Failed'),
        ('unknown', 'Unknown'),
        ('maintenance', 'Maintenance'),
    ]
    TRANSPORT_CHOICES = [
        ('auto', 'Auto-detect'),
        ('https', 'HTTPS'),
        ('http', 'HTTP'),
        ('ssh', 'SSH only'),
    ]

    # Core fields
    ip_address = models.CharField(max_length=255,
                                   help_text='IPv4, hostname, or FQDN (e.g. 10.0.0.1 or switch1.lab.local)')
    mgmt_ipv6 = models.CharField(
        max_length=255, blank=True, default='',
        help_text='IPv6 management (DHCPv6 global, SLAAC fe80::…, or static)',
    )
    mgmt_ipv6_source = models.CharField(
        max_length=8, choices=MGMT_IPV6_SOURCE_CHOICES, default='',
        blank=True,
        help_text='How mgmt_ipv6 was assigned (dhcpv6, slaac, or static)',
    )
    preferred_ip_version = models.CharField(
        max_length=12, choices=PREFERRED_IP_VERSION_CHOICES, default='ipv4',
        help_text='Which management address to prefer for connections and UI labels',
    )
    hostname = models.CharField(max_length=255, blank=True, null=True)
    username = models.CharField(max_length=100)
    password = models.CharField(max_length=100)
    vendor_type = models.CharField(max_length=20, choices=VENDOR_CHOICES, default='arista',
                                   help_text="Device vendor / OS type")
    transport = models.CharField(max_length=5, choices=TRANSPORT_CHOICES, default='auto',
                                 help_text="Protocol for API communication")
    api_port = models.IntegerField(blank=True, null=True,
                                   help_text="Custom API port (leave blank for default)")
    api_key = models.CharField(max_length=500, blank=True, default='',
                               help_text="API key/token (FortiGate or Palo Alto)")
    enable_password = models.CharField(max_length=100, blank=True, default='',
                                       help_text="Enable/privilege password if required")
    snmp_community = models.CharField(max_length=100, blank=True, default='public',
                                      help_text="SNMP community for Keysight/SNMP-based devices")

    # Auto-discovered fields
    version = models.CharField(max_length=100, blank=True, null=True)
    serial_number = models.CharField(max_length=100, blank=True, null=True)
    model_name = models.CharField(max_length=100, blank=True, null=True)
    mac_address = models.CharField(max_length=50, blank=True, null=True)
    uptime = models.FloatField(blank=True, null=True, help_text="Uptime in seconds")
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='unknown')
    last_seen = models.DateTimeField(blank=True, null=True)

    # Organization fields
    tags = models.CharField(max_length=500, blank=True, default='', help_text="Comma-separated tags")
    notes = models.TextField(blank=True, default='')
    site = models.CharField(max_length=200, blank=True, default='', help_text="Site/location name")
    rack = models.CharField(max_length=100, blank=True, default='', help_text="Rack position")
    group_name = models.CharField(max_length=200, blank=True, default='',
                                  help_text="Device group (e.g. core, distribution, access)")
    contact = models.CharField(max_length=200, blank=True, default='',
                               help_text="Contact person/team for this device")
    maintenance_mode = models.BooleanField(default=False,
                                           help_text="Device is in maintenance window")
    device_group = models.ForeignKey('DeviceGroup', on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='devices')

    # Per-OS credentials (optional — overrides username/password for specific stacks)
    arista_username = models.CharField(max_length=100, blank=True, default='',
                                       help_text="Arista EOS login (overrides username if set)")
    arista_password = models.CharField(max_length=100, blank=True, default='',
                                       help_text="Arista EOS password (overrides password if set)")
    sonic_username = models.CharField(max_length=100, blank=True, default='',
                                      help_text="SONiC login (overrides username if set)")
    sonic_password = models.CharField(max_length=100, blank=True, default='',
                                      help_text="SONiC password (overrides password if set)")

    # Dual-OS rotation support (e.g. Arista boxes that alternate EOS/SONiC every N hours)
    vendor_type_secondary = models.CharField(
        max_length=20, blank=True, default='',
        choices=[('', 'None'), ('sonic', 'SONiC'), ('arista', 'Arista EOS')],
        help_text="Secondary OS vendor type for boxes that rotate between two OSes (e.g. arista↔sonic)"
    )
    dual_os_mode = models.BooleanField(
        default=False,
        help_text=(
            "EOS/SONiC interchangeable mode: when ON, LabVault auto-detects which OS is "
            "active and connects with the matching credentials. "
            "EOS: arista_username/arista_password  SONiC: sonic_username/sonic_password."
        )
    )

    # Alerting thresholds
    cpu_threshold = models.IntegerField(default=80, help_text="CPU alert threshold (%)")
    memory_threshold = models.IntegerField(default=85, help_text="Memory alert threshold (%)")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['ip_address']

    def __str__(self):
        return f"{self.hostname or self.connect_address or self.ip_address}"

    @property
    def effective_mgmt_ipv6(self) -> str:
        """IPv6 for labels/topology (may include DHCPv6 derive for OCS lab)."""
        from .ip_addressing import resolve_mgmt_ipv6
        return resolve_mgmt_ipv6(
            ipv4=self.ip_address,
            mgmt_ipv6=self.mgmt_ipv6,
            ipv6_source=self.mgmt_ipv6_source,
        )

    @property
    def connect_mgmt_ipv6(self) -> str:
        """IPv6 used for live connections — stored/static/SLAAC only, not formula-derived."""
        from .ip_addressing import resolve_mgmt_ipv6_for_connect
        return resolve_mgmt_ipv6_for_connect(
            ipv4=self.ip_address,
            mgmt_ipv6=self.mgmt_ipv6,
            ipv6_source=self.mgmt_ipv6_source,
        )

    @property
    def connect_address(self) -> str:
        from .ip_addressing import resolve_connect_address
        return resolve_connect_address(
            ipv4=self.ip_address,
            ipv6=self.connect_mgmt_ipv6,
            preferred=self.preferred_ip_version,
            hostname=self.hostname or '',
        )

    @property
    def connect_targets(self) -> list:
        from .ip_addressing import resolve_connect_targets
        return resolve_connect_targets(
            ipv4=self.ip_address,
            ipv6=self.connect_mgmt_ipv6,
            preferred=self.preferred_ip_version,
            hostname=self.hostname or '',
        )

    @property
    def address_summary(self) -> str:
        from .ip_addressing import display_addresses, ipv6_display_tag
        v6 = self.effective_mgmt_ipv6
        parts = []
        if (self.ip_address or '').strip():
            parts.append(f'v4 {self.ip_address.strip()}')
        if v6:
            parts.append(f'{ipv6_display_tag(v6, ipv6_source=self.mgmt_ipv6_source)} {v6}')
        host = (self.hostname or '').strip()
        if host and host not in (self.ip_address, v6):
            parts.append(host)
        return ' · '.join(parts) if parts else '—'

    @property
    def mgmt_display(self) -> str:
        from .ip_addressing import display_mgmt_address
        return display_mgmt_address(
            ipv4=self.ip_address,
            ipv6=self.effective_mgmt_ipv6,
            preferred=self.preferred_ip_version,
            hostname=self.hostname or '',
        )

    @property
    def mgmt_label(self) -> str:
        from .ip_addressing import mgmt_label_text
        return mgmt_label_text(
            ipv4=self.ip_address,
            ipv6=self.effective_mgmt_ipv6,
            preferred=self.preferred_ip_version,
            hostname=self.hostname or '',
        )

    @property
    def mgmt_ip_fields(self) -> dict:
        """Dual-stack fields for topology JSON, APIs, and templates."""
        from .ip_addressing import mgmt_ip_bundle
        return mgmt_ip_bundle(
            ipv4=self.ip_address,
            ipv6=self.effective_mgmt_ipv6,
            preferred=self.preferred_ip_version,
            hostname=self.hostname or '',
            ipv6_source=self.mgmt_ipv6_source,
        )

    @property
    def tag_list(self):
        if self.tags:
            return [t.strip() for t in self.tags.split(',') if t.strip()]
        return []

    @property
    def vendor_display(self):
        return dict(self.VENDOR_CHOICES).get(self.vendor_type, self.vendor_type)

    @property
    def vendor_icon(self):
        icons = {
            'arista': 'fa-leaf',
            'sonic': 'fa-volume-up',
            'fortigate': 'fa-shield-alt',
            'paloalto': 'fa-fire',
            'keysight': 'fa-microchip',
            'ocs': 'fa-project-diagram',
        }
        return icons.get(self.vendor_type, 'fa-server')

    @property
    def vendor_color(self):
        colors = {
            'arista': '#2196F3',
            'sonic': '#4CAF50',
            'fortigate': '#FF5722',
            'paloalto': '#FF9800',
            'keysight': '#9C27B0',
            'ocs': '#06b6d4',
        }
        return colors.get(self.vendor_type, '#607D8B')

    @property
    def uptime_display(self):
        if self.uptime is None:
            return 'N/A'
        seconds = int(self.uptime)
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"

    @property
    def is_firewall(self):
        return self.vendor_type in ('fortigate', 'paloalto')


class DeviceGroup(models.Model):
    """Hierarchical device grouping for fleet management."""
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True, default='')
    parent_group = models.ForeignKey('self', on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='children')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def device_count(self):
        return self.devices.count()

    @property
    def full_path(self):
        """Return group hierarchy path like 'DataCenter/Core/Spine'."""
        parts = [self.name]
        parent = self.parent_group
        while parent:
            parts.insert(0, parent.name)
            parent = parent.parent_group
        return '/'.join(parts)


class AuditLog(models.Model):
    """User-initiated action trail written by ``views._log_action`` (login, device CRUD, backups, ...).

    ``action`` is not validated on insert, so some writers use values outside
    ``ACTION_CHOICES`` (e.g. ``import``, ``ocs_snapshot_*``).
    """

    ACTION_CHOICES = [
        ('device_add', 'Device Added'),
        ('device_delete', 'Device Deleted'),
        ('device_edit', 'Device Edited'),
        ('command_exec', 'Command Executed'),
        ('config_change', 'Configuration Changed'),
        ('config_backup', 'Configuration Backed Up'),
        ('login', 'User Login'),
        ('logout', 'User Logout'),
        ('vlan_create', 'VLAN Created'),
        ('vlan_delete', 'VLAN Deleted'),
        ('interface_change', 'Interface Changed'),
        ('bulk_action', 'Bulk Action'),
        ('alert_triggered', 'Alert Triggered'),
        ('maintenance_toggle', 'Maintenance Toggled'),
        ('webhook_sent', 'Webhook Sent'),
        ('scheduled_job', 'Scheduled Job'),
        ('compliance_check', 'Compliance Check'),
    ]

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    device = models.ForeignKey(Device, on_delete=models.SET_NULL, null=True, blank=True)
    details = models.TextField(blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"[{self.timestamp}] {self.get_action_display()} by {self.user}"


class DeviceSnapshot(models.Model):
    """Stores periodic health snapshots for trending/history."""
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='snapshots')
    cpu_utilization = models.FloatField(null=True, blank=True)
    memory_used = models.FloatField(null=True, blank=True)
    memory_total = models.FloatField(null=True, blank=True)
    temperature = models.FloatField(null=True, blank=True)
    uptime = models.FloatField(null=True, blank=True)
    interface_up_count = models.IntegerField(null=True, blank=True)
    interface_down_count = models.IntegerField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"Snapshot for {self.device} at {self.timestamp}"

    @property
    def memory_percent(self):
        if self.memory_used and self.memory_total and self.memory_total > 0:
            return round((self.memory_used / self.memory_total) * 100, 1)
        return None


class Alert(models.Model):
    """Alert/notification model for device events."""
    SEVERITY_CHOICES = [
        ('critical', 'Critical'),
        ('warning', 'Warning'),
        ('info', 'Info'),
    ]
    TYPE_CHOICES = [
        ('device_down', 'Device Down'),
        ('device_up', 'Device Came Online'),
        ('high_cpu', 'High CPU Usage'),
        ('high_memory', 'High Memory Usage'),
        ('interface_down', 'Interface Down'),
        ('auth_failure', 'Authentication Failure'),
        ('config_change', 'Config Change Detected'),
        ('threshold_breach', 'Threshold Breach'),
        ('bgp_down', 'BGP Peer Down'),
        ('ospf_down', 'OSPF Neighbor Down'),
        ('environment', 'Environmental Alert'),
    ]

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='alerts')
    alert_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default='warning')
    message = models.TextField()
    acknowledged = models.BooleanField(default=False)
    acknowledged_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.severity}] {self.get_alert_type_display()} - {self.device}"


class ConfigBackup(models.Model):
    """Stores configuration backups for devices."""
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='config_backups')
    config_type = models.CharField(max_length=20, default='running',
                                   choices=[('running', 'Running'), ('startup', 'Startup')])
    content = models.TextField()
    diff_from_previous = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Config backup for {self.device} ({self.config_type}) at {self.created_at}"


class ComplianceRule(models.Model):
    """Configuration compliance rules that devices should match."""
    CATEGORY_CHOICES = [
        ('ntp', 'NTP'),
        ('aaa', 'AAA/Authentication'),
        ('logging', 'Logging/Syslog'),
        ('snmp', 'SNMP'),
        ('security', 'Security'),
        ('routing', 'Routing'),
        ('management', 'Management'),
        ('custom', 'Custom'),
    ]

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='custom')
    vendor_type = models.CharField(max_length=20, choices=Device.VENDOR_CHOICES, default='arista',
                                   blank=True)
    pattern = models.TextField(help_text="Regex pattern to search in running config")
    should_exist = models.BooleanField(default=True,
                                       help_text="True=pattern should exist, False=should NOT exist")
    severity = models.CharField(max_length=10, choices=Alert.SEVERITY_CHOICES, default='warning')
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class ComplianceResult(models.Model):
    """Stores results of compliance checks per device."""
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='compliance_results')
    rule = models.ForeignKey(ComplianceRule, on_delete=models.CASCADE)
    passed = models.BooleanField(default=True)
    details = models.TextField(blank=True, default='')
    checked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-checked_at']


class SavedCommand(models.Model):
    """User-saved frequently used commands."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='saved_commands')
    name = models.CharField(max_length=200)
    command = models.TextField()
    vendor_type = models.CharField(max_length=20, choices=Device.VENDOR_CHOICES, blank=True, default='')
    description = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


# ============================================================
# NEW MODELS - Topology, Interface Trending, Scheduling, etc.
# ============================================================

class TopologyLink(models.Model):
    """Represents a discovered network link between two devices (LLDP/CDP)."""
    DISCOVERY_CHOICES = [
        ('lldp', 'LLDP'),
        ('cdp', 'CDP'),
        ('manual', 'Manual'),
    ]

    device_a = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='links_as_a')
    port_a = models.CharField(max_length=100, help_text="Local port on device A")
    device_b = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='links_as_b')
    port_b = models.CharField(max_length=100, help_text="Remote port on device B")
    speed = models.CharField(max_length=20, blank=True, default='')
    duplex = models.CharField(
        max_length=20, blank=True, default='',
        help_text='full, half, or empty if unknown',
    )
    lag = models.CharField(max_length=100, blank=True, default='',
                          help_text="Port-channel / LAG name if member")
    discovered_via = models.CharField(max_length=10, choices=DISCOVERY_CHOICES, default='lldp')
    link_status = models.CharField(max_length=10, default='up',
                                   choices=[('up', 'Up'), ('down', 'Down')])
    last_seen = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-last_seen']
        unique_together = [('device_a', 'port_a', 'device_b', 'port_b')]

    def __str__(self):
        return f"{self.device_a}:{self.port_a} <-> {self.device_b}:{self.port_b}"


class ChassisDeviceLink(models.Model):
    """LLDP link between a Keysight chassis and a Device (switch)."""
    chassis = models.ForeignKey('KeysightChassis', on_delete=models.CASCADE, related_name='topology_links')
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='chassis_links')
    port_chassis = models.CharField(max_length=100, help_text='Port on chassis')
    port_device = models.CharField(max_length=100, help_text='Port on device')
    discovered_via = models.CharField(max_length=10, default='lldp')
    link_status = models.CharField(max_length=10, default='up', choices=[('up', 'Up'), ('down', 'Down')])
    last_seen = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-last_seen']
        unique_together = [('chassis', 'port_chassis', 'device', 'port_device')]

    def __str__(self):
        return f"{self.chassis}:{self.port_chassis} <-> {self.device}:{self.port_device}"


class InterfaceSnapshot(models.Model):
    """Interface-level counters for bandwidth/error trending."""
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='interface_snapshots')
    interface_name = models.CharField(max_length=100)
    status = models.CharField(max_length=10, default='unknown')
    bandwidth_in = models.BigIntegerField(default=0, help_text="Bytes received")
    bandwidth_out = models.BigIntegerField(default=0, help_text="Bytes transmitted")
    packets_in = models.BigIntegerField(default=0)
    packets_out = models.BigIntegerField(default=0)
    errors_in = models.BigIntegerField(default=0)
    errors_out = models.BigIntegerField(default=0)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['device', 'interface_name', '-timestamp']),
        ]

    def __str__(self):
        return f"{self.device}:{self.interface_name} @ {self.timestamp}"


class ScheduledJob(models.Model):
    """Scheduled/recurring task like config backup, compliance check."""
    JOB_TYPES = [
        ('config_backup', 'Config Backup'),
        ('compliance_check', 'Compliance Check'),
        ('topology_scan', 'Topology Scan'),
        ('health_snapshot', 'Health Snapshot'),
        ('interface_counters', 'Interface Counters'),
    ]
    SCHEDULE_CHOICES = [
        ('15min', 'Every 15 Minutes'),
        ('hourly', 'Hourly'),
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
    ]

    name = models.CharField(max_length=200)
    job_type = models.CharField(max_length=30, choices=JOB_TYPES)
    schedule = models.CharField(max_length=10, choices=SCHEDULE_CHOICES, default='daily')
    last_run = models.DateTimeField(null=True, blank=True)
    next_run = models.DateTimeField(null=True, blank=True)
    enabled = models.BooleanField(default=True)
    target_devices = models.TextField(blank=True, default='',
                                     help_text="Comma-separated device IDs or 'all'")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.get_schedule_display()})"

    @property
    def is_due(self):
        if not self.enabled:
            return False
        if self.next_run is None:
            return True
        return timezone.now() >= self.next_run


class MaintenanceWindow(models.Model):
    """Scheduled maintenance period that suppresses alerts and optionally skips polling."""
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    recurring = models.BooleanField(default=False)
    recurrence_pattern = models.CharField(max_length=50, blank=True, default='',
                                         help_text="e.g. 'weekly', 'monthly'")
    suppress_alerts = models.BooleanField(default=True)
    skip_polling = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-start_time']

    def __str__(self):
        return f"{self.name}: {self.start_time} - {self.end_time}"

    @property
    def is_active(self):
        now = timezone.now()
        return self.start_time <= now <= self.end_time

    def get_devices(self):
        """Return devices associated with this window via M2M."""
        return self.maintenance_devices.all()


class MaintenanceWindowDevice(models.Model):
    """M2M between MaintenanceWindow and Device."""
    window = models.ForeignKey(MaintenanceWindow, on_delete=models.CASCADE,
                              related_name='maintenance_devices')
    device = models.ForeignKey(Device, on_delete=models.CASCADE,
                              related_name='maintenance_windows')

    class Meta:
        unique_together = [('window', 'device')]


class WebhookEndpoint(models.Model):
    """Webhook endpoint for alert notifications (Slack, Teams, PagerDuty, etc.)."""
    WEBHOOK_TYPES = [
        ('slack', 'Slack'),
        ('teams', 'Microsoft Teams'),
        ('pagerduty', 'PagerDuty'),
        ('generic', 'Generic HTTP'),
    ]

    name = models.CharField(max_length=200)
    webhook_type = models.CharField(max_length=20, choices=WEBHOOK_TYPES, default='generic')
    url = models.URLField(max_length=500)
    secret = models.CharField(max_length=200, blank=True, default='',
                             help_text="Secret/token for authentication")
    enabled = models.BooleanField(default=True)
    severity_filter = models.CharField(max_length=50, blank=True, default='',
                                      help_text="Comma-separated: critical,warning,info")
    last_triggered = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.get_webhook_type_display()})"


class APIToken(models.Model):
    """REST API token for external integrations."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='api_tokens')
    name = models.CharField(max_length=200)
    token = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.user.username})"

    @property
    def is_valid(self):
        if not self.enabled:
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        return True


# ============================================================
# KEYSIGHT / IXIA MODELS
# ============================================================

class KeysightChassis(models.Model):
    """Represents an Ixia/Keysight test chassis."""
    ip_address = models.CharField(max_length=255, unique=True,
                                  help_text='IPv4, hostname, or FQDN (management)')
    mgmt_ipv6 = models.CharField(
        max_length=255, blank=True, default='',
        help_text='IPv6 management (DHCPv6 global, SLAAC, or static)',
    )
    mgmt_ipv6_source = models.CharField(
        max_length=8, choices=MGMT_IPV6_SOURCE_CHOICES, default='',
        blank=True,
    )
    preferred_ip_version = models.CharField(
        max_length=12, choices=PREFERRED_IP_VERSION_CHOICES, default='ipv4',
        help_text='Prefer IPv4 or IPv6 when connecting to chassis APIs',
    )
    username = models.CharField(max_length=100, default='admin')
    password = models.CharField(max_length=200, default='admin')
    chassis_type = models.CharField(max_length=24, choices=KEYSIGHT_CHASSIS_TYPE_CHOICES, default='xgs12')

    # Auto-discovered fields
    hostname = models.CharField(max_length=200, blank=True, default='')
    serial_number = models.CharField(max_length=100, blank=True, default='')
    controller_serial = models.CharField(max_length=100, blank=True, default='')
    ixos_version = models.CharField(max_length=100, blank=True, default='')
    chassis_state = models.CharField(max_length=30, choices=KEYSIGHT_CHASSIS_STATE_CHOICES, default='unknown')
    num_slots = models.IntegerField(default=0)
    num_physical_cards = models.IntegerField(default=0)
    os_type = models.CharField(max_length=20, blank=True, default='')
    os_platform = models.CharField(max_length=20, choices=KEYSIGHT_OS_PLATFORM_CHOICES, default='unknown')
    kcos_version = models.CharField(max_length=100, blank=True, default='')

    # Connection status
    status = models.CharField(max_length=20, choices=KEYSIGHT_STATUS_CHOICES, default='unknown')
    last_seen = models.DateTimeField(null=True, blank=True)

    # Organization
    site = models.CharField(max_length=100, blank=True, default='')
    geo_location = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Geographic site e.g. Building A, Lab 2',
    )
    lab_name = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Lab identifier e.g. PM Lab, Regression Lab, Apps Lab',
    )
    tags = models.CharField(max_length=500, blank=True, default='')
    team_tags = models.CharField(
        max_length=500, blank=True, default='',
        help_text='Comma-separated team ownership tags, e.g. QA,QA/L47,KCOS-Team',
    )
    notes = models.TextField(blank=True, default='')
    hardware_error_reported = models.BooleanField(
        default=False,
        help_text='Operator-flagged known hardware issue (missing QATs, bad slot, etc.)',
    )
    snmp_community = models.CharField(max_length=100, blank=True, default='public',
                                      help_text='SNMP community for LLDP discovery (topology)')

    @property
    def team_tags_list(self) -> list[str]:
        """Return team_tags as a cleaned list, empty strings removed."""
        return [t.strip() for t in self.team_tags.split(',') if t.strip()]

    # IxOS applications (stored as JSON text)
    ixos_applications = models.TextField(blank=True, default='{}')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Keysight Chassis'
        verbose_name_plural = 'Keysight Chassis'
        ordering = ['ip_address']

    @property
    def chassis_icon(self):
        icons = {
            'xgs2': 'fa-server', 'xgs12': 'fa-server',
            'xg': 'fa-hdd', 'xm': 'fa-hdd',
            'aps_m1010': 'fa-microchip', 'aps_m8400': 'fa-microchip',
            'aps_standalone': 'fa-microchip',
            'aresone': 'fa-network-wired',
            'aresone_htrex': 'fa-bolt',
            'trex': 'fa-bolt',
            'ixvm': 'fa-cloud', 'other': 'fa-question-circle',
        }
        return icons.get(self.chassis_type, 'fa-server')

    def __str__(self):
        label = self.hostname or self.connect_address or self.ip_address
        return f'{label} ({self.get_chassis_type_display()})'

    @property
    def effective_mgmt_ipv6(self) -> str:
        """IPv6 for labels/topology (may include DHCPv6 derive for OCS lab)."""
        from .ip_addressing import resolve_mgmt_ipv6
        return resolve_mgmt_ipv6(
            ipv4=self.ip_address,
            mgmt_ipv6=self.mgmt_ipv6,
            ipv6_source=self.mgmt_ipv6_source,
        )

    @property
    def connect_mgmt_ipv6(self) -> str:
        """IPv6 used for live connections — stored/static/SLAAC only, not formula-derived."""
        from .ip_addressing import resolve_mgmt_ipv6_for_connect
        return resolve_mgmt_ipv6_for_connect(
            ipv4=self.ip_address,
            mgmt_ipv6=self.mgmt_ipv6,
            ipv6_source=self.mgmt_ipv6_source,
        )

    @property
    def connect_address(self) -> str:
        from .ip_addressing import resolve_connect_address
        return resolve_connect_address(
            ipv4=self.ip_address,
            ipv6=self.connect_mgmt_ipv6,
            preferred=self.preferred_ip_version,
            hostname=self.hostname or '',
        )

    @property
    def connect_targets(self) -> list:
        from .ip_addressing import resolve_connect_targets
        return resolve_connect_targets(
            ipv4=self.ip_address,
            ipv6=self.connect_mgmt_ipv6,
            preferred=self.preferred_ip_version,
            hostname=self.hostname or '',
        )

    @property
    def address_summary(self) -> str:
        from .ip_addressing import ipv6_display_tag
        v6 = self.effective_mgmt_ipv6
        parts = []
        if (self.ip_address or '').strip():
            parts.append(f'v4 {self.ip_address.strip()}')
        if v6:
            parts.append(f'{ipv6_display_tag(v6, ipv6_source=self.mgmt_ipv6_source)} {v6}')
        host = (self.hostname or '').strip()
        if host and host not in (self.ip_address, v6):
            parts.append(host)
        return ' · '.join(parts) if parts else '—'

    @property
    def mgmt_display(self) -> str:
        from .ip_addressing import display_mgmt_address
        return display_mgmt_address(
            ipv4=self.ip_address,
            ipv6=self.effective_mgmt_ipv6,
            preferred=self.preferred_ip_version,
            hostname=self.hostname or '',
        )

    @property
    def mgmt_label(self) -> str:
        from .ip_addressing import mgmt_label_text
        return mgmt_label_text(
            ipv4=self.ip_address,
            ipv6=self.effective_mgmt_ipv6,
            preferred=self.preferred_ip_version,
            hostname=self.hostname or '',
        )

    @property
    def mgmt_ip_fields(self) -> dict:
        from .ip_addressing import mgmt_ip_bundle
        return mgmt_ip_bundle(
            ipv4=self.ip_address,
            ipv6=self.effective_mgmt_ipv6,
            preferred=self.preferred_ip_version,
            hostname=self.hostname or '',
            ipv6_source=self.mgmt_ipv6_source,
        )


class KeysightChassisSnapshot(models.Model):
    """Time-series health metrics for a Keysight chassis."""
    chassis = models.ForeignKey(KeysightChassis, on_delete=models.CASCADE, related_name='snapshots')
    cpu_utilization = models.FloatField(default=0)
    memory_used = models.BigIntegerField(default=0)
    memory_total = models.BigIntegerField(default=0)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f'{self.chassis} @ {self.timestamp:%Y-%m-%d %H:%M}'


class NPResourceSample(models.Model):
    """Per-slot/NP CPU and memory sample for time-series monitoring.

    Stored in the separate ``np_timeseries`` database (see NPTimeseriesRouter).
    ``db_constraint=False`` because KeysightChassis lives on the default DB.
    """

    chassis = models.ForeignKey(
        KeysightChassis,
        on_delete=models.DO_NOTHING,
        related_name='np_samples',
        db_constraint=False,
    )
    slot_number = models.IntegerField()
    node_name = models.CharField(max_length=128, blank=True, default='')
    np_id = models.IntegerField(default=-1)  # -1 = aggregate for the slot/node
    cpu_percent = models.FloatField(default=0)
    memory_used_bytes = models.BigIntegerField(default=0)
    memory_total_bytes = models.BigIntegerField(default=0)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['chassis', 'timestamp']),
            models.Index(fields=['chassis', 'slot_number', 'np_id', 'timestamp']),
        ]

    def __str__(self):
        return f'{self.chassis_id} slot={self.slot_number} np={self.np_id} @ {self.timestamp}'


class PortUsageSample(models.Model):
    """Port/resource usage episode for cyclic utilization (``np_timeseries`` DB).

    LAAS posts run episodes; LabVault may also record reservation windows.
    Retention: 31 days (see ``cleanup_np_timeseries``).
    """

    USAGE_EVENT_CHOICES = [
        ('reservation', 'Reservation'),
        ('enqueue', 'Enqueue'),
        ('active', 'Active run'),
        ('complete', 'Complete'),
    ]

    topology_id = models.IntegerField(db_index=True)
    topology_node_id = models.IntegerField(null=True, blank=True, db_index=True)
    resource_key = models.CharField(max_length=256, db_index=True)
    port_label = models.CharField(max_length=128, blank=True, default='')
    ocs_triplet = models.CharField(max_length=64, blank=True, default='')
    chassis_id = models.IntegerField(null=True, blank=True, db_index=True)
    episode_id = models.CharField(max_length=128, db_index=True)
    event = models.CharField(max_length=32, choices=USAGE_EVENT_CHOICES, default='active')
    team = models.CharField(max_length=128, blank=True, default='')
    user_name = models.CharField(max_length=150, blank=True, default='')
    source = models.CharField(max_length=32, default='labvault')
    started_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['topology_id', 'started_at']),
            models.Index(fields=['topology_id', 'resource_key', 'started_at']),
            models.Index(fields=['episode_id', 'event']),
        ]

    def __str__(self):
        return f'topo={self.topology_id} {self.resource_key} {self.event} @ {self.started_at}'


class LabMetricSample(models.Model):
    """Numeric metric sample for topology resources (``np_timeseries`` DB).

    Collected every ~5 minutes by ``collect_topology_metrics``.
    Retention: 7 days raw (see ``cleanup_np_timeseries``); hourly rollups kept 31 days.
    """

    topology_id = models.IntegerField(db_index=True)
    resource_key = models.CharField(max_length=256, db_index=True)
    metric = models.CharField(max_length=32, db_index=True)
    value = models.FloatField(default=0)
    sampled_at = models.DateTimeField(db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['topology_id', 'sampled_at'], name='connect_lms_topo_ts'),
            models.Index(fields=['topology_id', 'metric', 'sampled_at']),
            models.Index(fields=['topology_id', 'resource_key', 'sampled_at']),
            models.Index(fields=['topology_id', 'resource_key'], name='connect_lms_topo_rkey'),
        ]

    def __str__(self):
        return f'topo={self.topology_id} {self.resource_key} {self.metric}={self.value} @ {self.sampled_at}'


class LabMetricRollup(models.Model):
    """Hourly pre-aggregated metric buckets (``np_timeseries`` DB).

    Updated by the collector after each write cycle. Retention: 31 days.
    """

    topology_id = models.IntegerField(db_index=True)
    resource_key = models.CharField(max_length=256)
    metric = models.CharField(max_length=32)
    bucket_start = models.DateTimeField(db_index=True)
    avg_value = models.FloatField(default=0)
    max_value = models.FloatField(default=0)
    min_value = models.FloatField(default=0)
    sample_count = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=['topology_id', 'bucket_start'], name='connect_lmr_topo_bucket'),
            models.Index(
                fields=['topology_id', 'resource_key', 'metric', 'bucket_start'],
                name='connect_lmr_topo_rkey_metric',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['topology_id', 'resource_key', 'metric', 'bucket_start'],
                name='connect_lmr_unique_bucket',
            ),
        ]

    def __str__(self):
        return (
            f'topo={self.topology_id} {self.resource_key} {self.metric} '
            f'avg={self.avg_value} @ {self.bucket_start}'
        )


class LabResourceEvent(models.Model):
    """Discrete resource state intervals (ownership, OCS patches, link up/down).

    Stored in ``np_timeseries``; retention 31 days.
    """

    EVENT_TYPE_CHOICES = [
        ('port_owned', 'Port owned'),
        ('port_released', 'Port released'),
        ('patch_add', 'Patch added'),
        ('patch_remove', 'Patch removed'),
        ('patch_move', 'Patch moved'),
        ('link_up', 'Link up'),
        ('link_down', 'Link down'),
        ('input_discard', 'Input discard'),
    ]

    topology_id = models.IntegerField(db_index=True)
    resource_key = models.CharField(max_length=256, db_index=True)
    event_type = models.CharField(max_length=32, choices=EVENT_TYPE_CHOICES, db_index=True)
    started_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['topology_id', 'event_type', 'started_at']),
            models.Index(fields=['topology_id', 'resource_key', 'started_at']),
            models.Index(
                fields=['topology_id', 'resource_key', 'event_type', 'ended_at'],
                name='connect_lre_topo_rkey_ev_end',
            ),
        ]

    def __str__(self):
        return f'topo={self.topology_id} {self.resource_key} {self.event_type} @ {self.started_at}'


class KeysightSubnetScan(models.Model):
    """Configuration for subnet-based Keysight chassis discovery."""
    subnet = models.CharField(max_length=50, unique=True,
                              help_text='CIDR notation e.g. 192.0.2.0/24')
    default_username = models.CharField(max_length=100, default='admin')
    default_password = models.CharField(max_length=200, default='admin')
    auto_scan = models.BooleanField(default=False, help_text='Enable recurring background scan')
    scan_interval = models.IntegerField(default=60, help_text='Minutes between scans')
    last_scan = models.DateTimeField(null=True, blank=True)
    discovered_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['subnet']

    def __str__(self):
        return f'{self.subnet} (auto={self.auto_scan})'


class KeysightReservation(models.Model):
    """Virtual reservation of Keysight hardware for a time window."""
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='keysight_reservations')
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    status = models.CharField(max_length=20, choices=KEYSIGHT_RESERVATION_STATUS_CHOICES, default='upcoming')
    email_notified = models.BooleanField(default=False)
    notification_emails = models.TextField(
        blank=True, default='',
        help_text='Comma-separated email addresses for notifications'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_time']
        verbose_name = 'Keysight Reservation'
        verbose_name_plural = 'Keysight Reservations'

    def __str__(self):
        return f'{self.title} ({self.user.username}) {self.start_time:%Y-%m-%d %H:%M} - {self.end_time:%Y-%m-%d %H:%M}'

    def _aware_window(self):
        """Return (start, end) as aware datetimes; coerce ISO strings from JSON import."""
        from django.utils.dateparse import parse_datetime

        def _as_dt(val):
            if val is None or val == '':
                return None
            if isinstance(val, str):
                parsed = parse_datetime(val)
                if parsed is None:
                    try:
                        from datetime import datetime as _dt
                        parsed = _dt.fromisoformat(val.replace('Z', '+00:00'))
                    except ValueError:
                        return None
                val = parsed
            if timezone.is_naive(val):
                val = timezone.make_aware(val, timezone.get_current_timezone())
            return val

        start = _as_dt(self.start_time)
        end = _as_dt(self.end_time)
        if start is not None:
            self.start_time = start
        if end is not None:
            self.end_time = end
        return start, end

    @property
    def is_active(self):
        now = timezone.now()
        start, end = self._aware_window()
        if start is None or end is None:
            return False
        return start <= now <= end and self.status != 'cancelled'

    @property
    def computed_status(self):
        """Compute the actual status based on current time."""
        if self.status == 'cancelled':
            return 'cancelled'
        now = timezone.now()
        start, end = self._aware_window()
        if start is None or end is None:
            return self.status or 'upcoming'
        if now < start:
            return 'upcoming'
        elif start <= now <= end:
            return 'active'
        else:
            return 'expired'

    def save(self, *args, **kwargs):
        # Normalize datetimes before status compare (JSON import may pass strings)
        self._aware_window()
        if self.status != 'cancelled':
            self.status = self.computed_status
        super().save(*args, **kwargs)


class KeysightReservationItem(models.Model):
    """Individual hardware item in a reservation (chassis, slot, or port level)."""
    reservation = models.ForeignKey(
        KeysightReservation, on_delete=models.CASCADE, related_name='items'
    )
    chassis = models.ForeignKey(
        KeysightChassis, on_delete=models.CASCADE, related_name='reservation_items'
    )
    slot_number = models.IntegerField(
        null=True, blank=True,
        help_text='Null = whole chassis reserved'
    )
    port_number = models.IntegerField(
        null=True, blank=True,
        help_text='Null = whole slot reserved'
    )
    notes = models.CharField(max_length=500, blank=True, default='')

    class Meta:
        ordering = ['chassis__ip_address', 'slot_number', 'port_number']
        verbose_name = 'Reservation Item'
        verbose_name_plural = 'Reservation Items'

    def __str__(self):
        label = f'{self.chassis.hostname or self.chassis.ip_address}'
        if self.slot_number is not None:
            label += f' / Slot {self.slot_number}'
        if self.port_number is not None:
            label += f' / Port {self.port_number}'
        return label

    @property
    def level(self):
        """Return granularity level: chassis, slot, or port."""
        if self.slot_number is None:
            return 'chassis'
        elif self.port_number is None:
            return 'slot'
        return 'port'


class KeysightDeploymentJob(models.Model):
    """Tracks a deployment/upgrade operation on a KCOS chassis."""
    chassis = models.ForeignKey(
        KeysightChassis, on_delete=models.CASCADE, related_name='deployment_jobs')
    job_type = models.CharField(max_length=10, choices=DEPLOY_JOB_TYPE_CHOICES)
    package_type = models.CharField(max_length=20, choices=DEPLOY_PACKAGE_TYPE_CHOICES)
    target_version = models.CharField(max_length=200, blank=True, default='')
    package_url = models.TextField(blank=True, default='')
    status = models.CharField(max_length=15, choices=DEPLOY_STATUS_CHOICES, default='pending')
    progress = models.IntegerField(default=0)
    message = models.TextField(blank=True, default='')
    batch_id = models.CharField(max_length=100, blank=True, default='',
                                help_text='Groups multi-chassis batch jobs')
    started_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    started_ip = models.GenericIPAddressField(null=True, blank=True)
    started_user_agent = models.CharField(max_length=500, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        host = self.chassis.hostname or self.chassis.ip_address
        return f'{self.get_package_type_display()} {self.target_version} on {host} [{self.status}]'


# ============================================================
# BMC ENDPOINTS + REQUEST LOG (all authenticated page visits)
# ============================================================

class KeysightBmcEndpoint(models.Model):
    """BMC endpoint for direct IPMI access, even when the host/chassis is down."""
    hostname = models.CharField(
        max_length=255, unique=True,
        help_text='BMC FQDN, e.g. chassis-bmc.example.com')
    ip_address = models.CharField(
        max_length=45, blank=True, default='',
        help_text='Resolved or manually-set BMC IP')
    source = models.CharField(max_length=20, choices=BMC_SOURCE_CHOICES, default='manual_import')
    chassis = models.ForeignKey(
        KeysightChassis, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='bmc_endpoints')
    node_name = models.CharField(max_length=200, blank=True, default='',
                                 help_text='KCOS node name this BMC belongs to')
    serial_number = models.CharField(
        max_length=100, blank=True, default='',
        help_text='BMC / board serial for cross-chassis tracking',
    )
    aps_gen = models.CharField(
        max_length=8, blank=True, default='',
        help_text='APS 1.0 / 1.5 from FRU or node name (10 / 15)',
    )
    fru_board_product = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Last known IPMI FRU board product name',
    )
    hardware_error_reported = models.BooleanField(
        default=False,
        help_text='Operator-flagged known hardware issue on this node (CN/mgmt)',
    )
    hardware_error_notes = models.TextField(
        blank=True, default='',
        help_text='Details for reported hardware issue (missing QATs, bad slot, etc.)',
    )
    operating_mode = models.CharField(
        max_length=24, choices=BMC_OPERATING_MODE_CHOICES, default='unknown',
        help_text='How this BMC is deployed (standalone vs chassis CN/mgmt)',
    )
    relocated_from_chassis = models.ForeignKey(
        KeysightChassis, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='bmc_relocated_from',
        help_text='Previous chassis when BMC moved between standalone and multi-node',
    )
    relocated_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When chassis association last changed',
    )
    username = models.CharField(max_length=100, blank=True, default='',
                                help_text='Override credentials (blank = use default)')
    password = models.CharField(max_length=200, blank=True, default='')
    last_seen = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['hostname']
        verbose_name = 'BMC Endpoint'
        verbose_name_plural = 'BMC Endpoints'

    def __str__(self):
        ip = self.ip_address or '(unresolved)'
        return f'{self.hostname} [{ip}]'


class RequestLog(models.Model):
    """Lightweight log of every authenticated HTTP request for access auditing."""
    user = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='request_logs')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=500)
    status_code = models.IntegerField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f'[{self.timestamp}] {self.method} {self.path} by {self.user} from {self.ip_address}'


# ============================================================
# LAB TOPOLOGY DESIGNER
# ============================================================

class LabTopology(models.Model):
    """A named lab topology (designer canvas); owns nodes, links, test setups and fabric snapshots.

    ``extra`` is free-form JSON (site data, layout, OCS mappings) read by
    ``connect.lab_topology_views`` and the topology helper modules.
    """

    SOURCE_CHOICES = [
        ('lldp', 'LLDP'),
        ('import', 'Import'),
        ('manual', 'Manual'),
    ]
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='manual')
    tags = models.CharField(max_length=500, blank=True, default='')
    extra = models.JSONField(default=dict, blank=True)
    metrics_collection_enabled = models.BooleanField(
        default=True,
        help_text='When false, skip traffic/status metric collection for this topology (saves np_timeseries disk).',
    )
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Lab Topology'
        verbose_name_plural = 'Lab Topologies'

    def __str__(self):
        return self.name


class LabTopologyNode(models.Model):
    """A node on a ``LabTopology`` canvas, optionally bound to an inventory ``Device``."""

    NODE_TYPE_CHOICES = [
        ('switch', 'Switch'),
        ('server', 'Server'),
        ('ocs', 'OCS Photonic'),
        ('chassis', 'Chassis'),
        ('firewall', 'Firewall'),
        ('generic', 'Generic'),
    ]
    topology = models.ForeignKey(LabTopology, on_delete=models.CASCADE, related_name='nodes')
    device = models.ForeignKey('Device', null=True, blank=True, on_delete=models.SET_NULL)
    # node_key: stable external identifier (import ID, e.g. "arista1", "aresone01")
    node_key = models.CharField(max_length=64, blank=True, default='')
    node_type = models.CharField(max_length=20, choices=NODE_TYPE_CHOICES, default='switch')
    label = models.CharField(max_length=200)
    x = models.FloatField(default=0)
    y = models.FloatField(default=0)
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['label']
        verbose_name = 'Lab Topology Node'

    def __str__(self):
        return f'{self.topology.name} / {self.label}'


class LabTopologyLink(models.Model):
    """A cabled or OCS-patched link between two nodes of the same topology."""

    CABLE_CHOICES = [
        ('dac', 'DAC'),
        ('optic', 'Optic'),
        ('direct', 'Direct'),
        ('ocs', 'OCS Patch'),
    ]
    topology = models.ForeignKey(LabTopology, on_delete=models.CASCADE, related_name='links')
    node_a = models.ForeignKey(LabTopologyNode, on_delete=models.CASCADE, related_name='links_a')
    port_a = models.CharField(max_length=100)
    node_b = models.ForeignKey(LabTopologyNode, on_delete=models.CASCADE, related_name='links_b')
    port_b = models.CharField(max_length=100)
    cable_type = models.CharField(max_length=10, choices=CABLE_CHOICES, default='dac')
    color = models.CharField(max_length=20, blank=True, default='')
    label = models.CharField(max_length=200, blank=True, default='')
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = 'Lab Topology Link'

    def __str__(self):
        return f'{self.node_a.label}:{self.port_a} ↔ {self.node_b.label}:{self.port_b}'


class TestSetupTemplate(models.Model):
    """Stores a named test setup requirements + computed resource plan."""
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('planned', 'Planned'),
        ('applied', 'Applied'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('error', 'Error'),
    ]
    TEST_TYPE_CHOICES = [
        ('full_mesh', 'Full Mesh'),
        ('p2p', 'Point-to-Point'),
        ('one_to_many', 'One-to-Many'),
        ('custom', 'Custom'),
    ]
    topology = models.ForeignKey(
        'LabTopology', on_delete=models.CASCADE, related_name='test_setups',
        null=True, blank=True,
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    # Requirements spec: chassis_node_ids, port_count, port_speed, test_type
    requirements = models.JSONField(default=dict, blank=True)
    # Computed plan: ocs_patches, switch_configs, chassis_ports, summary
    computed_plan = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Test Setup Template'

    def __str__(self):
        return self.name


class TestSetupRun(models.Model):
    """Tracks a single execution of a TestSetupTemplate."""
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('applied', 'Applied'),
        ('testing', 'Testing'),
        ('done', 'Done'),
        ('error', 'Error'),
    ]
    template = models.ForeignKey(
        TestSetupTemplate, on_delete=models.CASCADE, related_name='runs',
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='queued')
    # Steps progress: [{name, state, message, ts}]
    steps = models.JSONField(default=list, blank=True)
    log = models.TextField(blank=True, default='')
    result_summary = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Test Setup Run'

    def __str__(self):
        return f'{self.template.name} run {self.pk} [{self.status}]'


class OcsPatchSnapshot(models.Model):
    """Persistent snapshot of an OCS cross-connect (patch) state.

    Captures the raw cross-connect list returned by the OCS REST API so the
    full patch state can be restored at any point in time.  Each snapshot also
    stores the derived ``pairs`` list (port_a / port_b / name / dir / band)
    which is the minimal set of fields needed to recreate the patches via
    ``xconnect_badd``.
    """

    device = models.ForeignKey(
        Device,
        on_delete=models.CASCADE,
        related_name='ocs_patch_snapshots',
        limit_choices_to={'vendor_type': 'ocs'},
    )
    name = models.CharField(max_length=200, blank=True, default='')
    description = models.TextField(blank=True, default='')
    # Raw cross-connect list from the OCS REST API (list of dicts)
    raw_xconns = models.JSONField(default=list)
    # Simplified pairs list for quick display and restore
    patch_pairs = models.JSONField(default=list)
    patch_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'OCS Patch Snapshot'
        verbose_name_plural = 'OCS Patch Snapshots'

    def __str__(self):
        return (
            f'OCS snapshot "{self.name or self.pk}" for {self.device} '
            f'({self.patch_count} patches, {self.created_at:%Y-%m-%d %H:%M})'
        )


class LabTopologyFabricSnapshot(models.Model):
    """Pre-built Port Fabric / Fabric Map JSON for fast topology views."""

    KIND_CHOICES = [
        ('port_fabric_cold', 'Port Fabric (cached sources)'),
        ('port_fabric_warm', 'Port Fabric (live OCS)'),
        ('fabric_map_cold', 'Fabric Map (cached sources)'),
        ('fabric_map_warm', 'Fabric Map (live OCS)'),
    ]

    topology = models.ForeignKey(
        LabTopology,
        on_delete=models.CASCADE,
        related_name='fabric_snapshots',
    )
    kind = models.CharField(max_length=32, choices=KIND_CHOICES, db_index=True)
    revision = models.CharField(max_length=80, db_index=True)
    payload = models.JSONField(default=dict)
    build_ms = models.FloatField(default=0)
    meta = models.JSONField(default=dict, blank=True)
    built_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-built_at']
        verbose_name = 'Lab Topology Fabric Snapshot'
        verbose_name_plural = 'Lab Topology Fabric Snapshots'
        constraints = [
            models.UniqueConstraint(
                fields=['topology', 'kind'],
                name='uniq_lab_topology_fabric_kind',
            ),
        ]
        indexes = [
            models.Index(fields=['topology', 'kind', '-built_at']),
        ]

    def __str__(self):
        return f'{self.topology_id}/{self.kind} @ {self.built_at:%Y-%m-%d %H:%M}'


class TopologyAuditLog(models.Model):
    """Audit trail for topology edit operations (Phase 6)."""
    ACTIONS = [
        ('node_added', 'Node Added'), ('node_deleted', 'Node Deleted'), ('node_updated', 'Node Updated'),
        ('link_added', 'Link Added'), ('link_deleted', 'Link Deleted'), ('link_updated', 'Link Updated'),
        ('import', 'Topology Imported'), ('layout_saved', 'Layout Saved'),
        ('graph_refreshed', 'Graph Refreshed'),
    ]
    topology = models.ForeignKey('LabTopology', on_delete=models.CASCADE, related_name='audit_log', null=True, blank=True)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='topology_audit_entries')
    action = models.CharField(max_length=40, choices=ACTIONS, db_index=True)
    detail = models.TextField(blank=True, default='')
    extra_json = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Topology Audit Log'
        verbose_name_plural = 'Topology Audit Logs'

    def __str__(self):
        return f'{self.action} on topology #{self.topology_id} by {self.actor} at {self.created_at:%Y-%m-%d %H:%M}'


class LabvaultUserPrefs(models.Model):
    """Per-user UI preferences (column profiles, etc.)."""

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='labvault_prefs',
    )
    ui_column_profiles = models.JSONField(
        default=dict,
        blank=True,
        help_text='Map of surface id → column profile store (v2 JSON from ui_column_profiles.js)',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'LabVault user preferences'
        verbose_name_plural = 'LabVault user preferences'

    def __str__(self):
        return f'Prefs for {self.user}'


class LabvaultGlobalPrefs(models.Model):
    """Org-wide UI preferences (singleton row singleton_key=default)."""

    singleton_key = models.CharField(
        max_length=32,
        primary_key=True,
        default='default',
        editable=False,
    )
    ui_column_profiles = models.JSONField(
        default=dict,
        blank=True,
        help_text='Map of surface id → column profile store (shared org-wide)',
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='labvault_global_pref_updates',
    )

    class Meta:
        verbose_name = 'LabVault global preferences'
        verbose_name_plural = 'LabVault global preferences'

    def __str__(self):
        return 'LabVault global preferences'


class ChangeLogEvent(models.Model):
    """Unified timeline: status, topology, deploy, and config change events."""

    EVENT_TYPE_CHOICES = [
        ('status_down', 'Went Down'),
        ('status_up', 'Came Online'),
        ('bmc_down', 'BMC Unreachable'),
        ('bmc_up', 'BMC Reachable'),
        ('temporarily_down', 'Temporarily Down'),
        ('extended_down', 'Extended Down'),
        ('moved_topology', 'Topology Changed'),
        ('moved_location', 'Location Changed'),
        ('moved_ip', 'IP Changed'),
        ('deploy_start', 'Deploy Started'),
        ('deploy_done', 'Deploy Completed'),
        ('deploy_failed', 'Deploy Failed'),
        ('upgrade', 'Upgrade'),
        ('downgrade', 'Downgrade'),
        ('config_change', 'Config Change'),
    ]
    TARGET_KIND_CHOICES = [
        ('device', 'Device'),
        ('chassis', 'Chassis'),
        ('bmc', 'BMC'),
    ]
    SOURCE_CHOICES = [
        ('system', 'System'),
        ('user', 'User'),
    ]

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    event_type = models.CharField(max_length=40, choices=EVENT_TYPE_CHOICES, db_index=True)
    target_kind = models.CharField(max_length=16, choices=TARGET_KIND_CHOICES, db_index=True)
    target_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    target_repr = models.CharField(max_length=500, blank=True, default='')
    detail = models.TextField(blank=True, default='')
    old_value = models.CharField(max_length=500, blank=True, default='')
    new_value = models.CharField(max_length=500, blank=True, default='')
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default='system')
    actor_username = models.CharField(max_length=150, blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True, default='')
    extra_json = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.event_type} {self.target_kind}:{self.target_id}'


class DeviceStateBaseline(models.Model):
    """Last-known state for change-detection scanners."""

    TARGET_KIND_CHOICES = ChangeLogEvent.TARGET_KIND_CHOICES

    target_kind = models.CharField(max_length=16, choices=TARGET_KIND_CHOICES)
    target_id = models.PositiveIntegerField()
    target_repr = models.CharField(max_length=500, blank=True, default='')
    ip = models.CharField(max_length=64, blank=True, default='')
    lab = models.CharField(max_length=200, blank=True, default='')
    geo = models.CharField(max_length=200, blank=True, default='')
    site = models.CharField(max_length=200, blank=True, default='')
    rack = models.CharField(max_length=100, blank=True, default='')
    peer_summary = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, blank=True, default='')
    last_offline_at = models.DateTimeField(null=True, blank=True)
    last_emitted_state = models.CharField(max_length=32, blank=True, default='')
    last_seen_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['target_kind', 'target_id'],
                name='connect_devicestatebaseline_kind_id_uniq',
            ),
        ]

    def __str__(self):
        return f'{self.target_kind}:{self.target_id}'




class RuntimeSetting(models.Model):
    """Versioned key → JSON operator setting, edited at runtime (see ``connect.runtime_settings``)."""

    key = models.CharField(max_length=64, unique=True)
    value_json = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ['key']
    def __str__(self):
        return f'{self.key}=v{self.version}'


class CliInvocation(models.Model):
    """One LabVault CLI command invocation (web or SSH), with redacted args/result for audit."""

    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='cli_invocations')
    source = models.CharField(max_length=16, default='web')
    command = models.CharField(max_length=128)
    args_redacted = models.JSONField(default=dict)
    outcome = models.CharField(max_length=32, default='ok')
    idempotency_key = models.CharField(max_length=64, blank=True, default='')
    correlation_id = models.CharField(max_length=64, blank=True, default='')
    duration_ms = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=512, blank=True, default='')
    target = models.CharField(max_length=128, blank=True, default='')
    result_state = models.CharField(max_length=32, blank=True, default='')
    result_redacted = models.JSONField(default=dict, blank=True)
    remote_addr = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        permissions = [
            ('control_labvault_services', 'Can control LabVault appliance services via CLI'),
        ]


class CliJob(models.Model):
    """Queued long-running CLI command, polled via ``/api/cli/v1/jobs/<id>/``."""

    STATUS_CHOICES = [('queued','Queued'),('running','Running'),('succeeded','Succeeded'),('failed','Failed')]
    command = models.CharField(max_length=128)
    args_redacted = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='queued')
    result_redacted = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=64, blank=True, default='', db_index=True)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='cli_jobs')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ['-created_at']


class CliAuthThrottle(models.Model):
    """Persistent SSH auth failure tracking (survives daemon restart)."""
    username = models.CharField(max_length=150, db_index=True)
    remote_addr = models.CharField(max_length=64, db_index=True)
    failure_count = models.PositiveIntegerField(default=0)
    first_failure_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('username', 'remote_addr')]
        ordering = ['-updated_at']
