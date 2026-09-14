# connect/forms.py

from django import forms
from .models import (
    Device, ComplianceRule, KeysightChassis,
    CONNECT_VIA_UI_CHOICES, MGMT_IPV6_SOURCE_CHOICES,
)
from .ip_addressing import normalize_ip, resolve_mgmt_ipv6


class MgmtIpv6FormMixin:
    """Validate optional IPv6 on device/chassis forms."""

    def clean_mgmt_ipv6(self):
        raw = (self.cleaned_data.get('mgmt_ipv6') or '').strip()
        if not raw:
            return ''
        normalized = normalize_ip(raw)
        if not normalized:
            raise forms.ValidationError('Enter a valid IPv6 address (e.g. fe80::1 or 2001:db8::1).')
        return normalized


class ConnectViaFormMixin:
    """Four explicit Connect-via modes; optional IPv6 source for DHCP vs static."""

    preferred_ip_version = forms.ChoiceField(
        choices=CONNECT_VIA_UI_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
        label='Connect via',
        help_text='Prefer IPv4, IPv6, SLAAC link-local, or dual-stack failover.',
    )
    mgmt_ipv6_source = forms.ChoiceField(
        choices=MGMT_IPV6_SOURCE_CHOICES,
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
        label='IPv6 source',
        help_text='DHCPv6: leave IPv6 blank — LabVault shows inferred lab address only.',
    )

    def clean(self):
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        src = (cleaned.get('mgmt_ipv6_source') or '').strip()
        v6 = (cleaned.get('mgmt_ipv6') or '').strip()
        ip4 = (cleaned.get('ip_address') or '').strip()
        if src == 'dhcpv6' and v6:
            raise forms.ValidationError(
                'For DHCPv6, leave the IPv6 field empty — use IPv4/hostname for connect '
                'and only paste an observed global IPv6 when you have it from the wire.',
            )
        if src == 'dhcpv6' and ip4:
            cleaned['mgmt_ipv6_display_hint'] = resolve_mgmt_ipv6(
                ipv4=ip4, mgmt_ipv6='', ipv6_source='dhcpv6',
            )
        return cleaned


class ConnectionForm(ConnectViaFormMixin, MgmtIpv6FormMixin, forms.ModelForm):
    """Form for adding a new device (any vendor)."""
    password = forms.CharField(widget=forms.PasswordInput(attrs={
        'class': 'form-control', 'placeholder': 'Device password',
    }))

    class Meta:
        model = Device
        fields = ['ip_address', 'hostname', 'mgmt_ipv6', 'mgmt_ipv6_source', 'preferred_ip_version',
                  'username', 'password', 'vendor_type', 'transport', 'api_port', 'api_key',
                  'enable_password', 'snmp_community', 'site', 'group_name', 'tags', 'notes']
        widgets = {
            'ip_address': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Management IPv4 e.g. 192.0.2.10',
            }),
            'hostname': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'FQDN when DNS works e.g. chassis1.lab.example',
            }),
            'mgmt_ipv6': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Observed global IPv6 only (leave blank for DHCPv6)',
            }),
            'username': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. admin',
            }),
            'vendor_type': forms.Select(attrs={'class': 'form-control'}),
            'transport': forms.Select(attrs={'class': 'form-control'}),
            'api_port': forms.NumberInput(attrs={
                'class': 'form-control', 'placeholder': 'Default: auto',
            }),
            'api_key': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'API key for FortiGate/Palo Alto (optional)',
            }),
            'enable_password': forms.PasswordInput(attrs={
                'class': 'form-control', 'placeholder': 'Enable password (optional)',
            }),
            'snmp_community': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'SNMP community (Keysight: default public)',
            }),
            'site': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. NYC-DC1',
            }),
            'group_name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. core, distribution',
            }),
            'tags': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. spine, datacenter-1',
            }),
            'notes': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 2,
                'placeholder': 'Optional notes about this device',
            }),
        }


class EditDeviceForm(ConnectViaFormMixin, MgmtIpv6FormMixin, forms.ModelForm):
    """Form for editing an existing device. Password shown in plain text."""
    password = forms.CharField(widget=forms.TextInput(attrs={
        'class': 'form-control', 'autocomplete': 'off',
    }))

    class Meta:
        model = Device
        fields = ['ip_address', 'hostname', 'mgmt_ipv6', 'mgmt_ipv6_source', 'preferred_ip_version',
                  'username', 'password', 'vendor_type', 'transport', 'api_port', 'api_key',
                  'enable_password', 'snmp_community', 'site', 'group_name', 'cpu_threshold',
                  'memory_threshold', 'maintenance_mode', 'vendor_type_secondary', 'dual_os_mode',
                  'arista_username', 'arista_password', 'sonic_username', 'sonic_password',
                  'contact', 'rack', 'tags', 'notes']
        widgets = {
            'ip_address': forms.TextInput(attrs={'class': 'form-control'}),
            'hostname': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'FQDN (preferred for connect when DNS works)',
            }),
            'mgmt_ipv6': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Observed IPv6 only — blank for DHCPv6',
            }),
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'vendor_type': forms.Select(attrs={'class': 'form-control'}),
            'transport': forms.Select(attrs={'class': 'form-control'}),
            'api_port': forms.NumberInput(attrs={'class': 'form-control'}),
            'api_key': forms.TextInput(attrs={'class': 'form-control'}),
            'enable_password': forms.TextInput(attrs={'class': 'form-control'}),
            'snmp_community': forms.TextInput(attrs={'class': 'form-control'}),
            'site': forms.TextInput(attrs={'class': 'form-control'}),
            'group_name': forms.TextInput(attrs={'class': 'form-control'}),
            'contact': forms.TextInput(attrs={'class': 'form-control'}),
            'rack': forms.TextInput(attrs={'class': 'form-control'}),
            'cpu_threshold': forms.NumberInput(attrs={'class': 'form-control'}),
            'memory_threshold': forms.NumberInput(attrs={'class': 'form-control'}),
            'maintenance_mode': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'dual_os_mode': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
            'vendor_type_secondary': forms.Select(attrs={'class': 'form-control form-select'}),
            'arista_username': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'admin'}),
            'arista_password': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'admin', 'autocomplete': 'off'}),
            'sonic_username': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'admin'}),
            'sonic_password': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'password', 'autocomplete': 'off'}),
            'tags': forms.TextInput(attrs={'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }


class CommandForm(forms.Form):
    command = forms.CharField(
        max_length=500,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter a command (e.g. show ip route)',
            'autocomplete': 'off',
        }),
    )


class DeviceImportForm(forms.Form):
    csv_file = forms.FileField(
        label='CSV File',
        help_text='CSV with headers: IP Address, Username, Password, Vendor, Tags, Notes, Site, Group',
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': '.csv'}),
    )


# ============================================================
# KEYSIGHT FORMS
# ============================================================

class KeysightChassisForm(ConnectViaFormMixin, MgmtIpv6FormMixin, forms.ModelForm):
    """Form for adding a new Keysight chassis."""
    password = forms.CharField(widget=forms.PasswordInput(attrs={
        'class': 'form-control', 'placeholder': 'Chassis password',
    }))

    class Meta:
        model = KeysightChassis
        fields = ['ip_address', 'hostname', 'mgmt_ipv6', 'mgmt_ipv6_source', 'preferred_ip_version',
                  'username', 'password', 'chassis_type', 'snmp_community', 'site', 'team_tags', 'notes',
                  'hardware_error_reported']
        widgets = {
            'ip_address': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Management IPv4 e.g. 192.0.2.31',
            }),
            'hostname': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'FQDN e.g. chassis1.lab.example',
            }),
            'mgmt_ipv6': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Observed IPv6 only — blank for DHCPv6',
            }),
            'snmp_community': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'SNMP community for LLDP topology (default: public)',
            }),
            'username': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. admin',
            }),
            'chassis_type': forms.Select(attrs={'class': 'form-control'}),
            'site': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'e.g. Lab-1',
            }),
            'team_tags': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. QA, QA/L47, KCOS-Team  (comma-separated)',
            }),
            'notes': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 2, 'placeholder': 'Optional notes',
            }),
        }
        labels = {
            'team_tags': 'Team Tags',
        }


class KeysightEditChassisForm(ConnectViaFormMixin, MgmtIpv6FormMixin, forms.ModelForm):
    """Form for editing an existing Keysight chassis."""
    password = forms.CharField(widget=forms.TextInput(attrs={
        'class': 'form-control', 'autocomplete': 'off',
    }))

    class Meta:
        model = KeysightChassis
        fields = ['ip_address', 'hostname', 'mgmt_ipv6', 'mgmt_ipv6_source', 'preferred_ip_version',
                  'username', 'password', 'chassis_type', 'snmp_community', 'site', 'team_tags', 'notes',
                  'hardware_error_reported']
        widgets = {
            'ip_address': forms.TextInput(attrs={'class': 'form-control'}),
            'hostname': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'FQDN when DNS is configured',
            }),
            'mgmt_ipv6': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Observed IPv6 only',
            }),
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'snmp_community': forms.TextInput(attrs={'class': 'form-control'}),
            'chassis_type': forms.Select(attrs={'class': 'form-control'}),
            'site': forms.TextInput(attrs={'class': 'form-control'}),
            'team_tags': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. QA, QA/L47, KCOS-Team  (comma-separated)',
            }),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'hardware_error_reported': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'team_tags': 'Team Tags',
            'hardware_error_reported': 'Reported hardware error',
        }
        help_texts = {
            'hardware_error_reported': 'Chassis-wide flag (edit chassis). Per-CN flags are on each node in chassis detail.',
            'notes': 'Describe the issue when hardware error is flagged.',
        }


class ComplianceRuleForm(forms.ModelForm):
    class Meta:
        model = ComplianceRule
        fields = ['name', 'description', 'vendor_type', 'pattern', 'should_exist', 'severity', 'enabled']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'vendor_type': forms.Select(attrs={'class': 'form-control'}),
            'pattern': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Regex pattern (e.g. ntp server \\d+)',
            }),
            'should_exist': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'severity': forms.Select(attrs={'class': 'form-control'}),
            'enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
