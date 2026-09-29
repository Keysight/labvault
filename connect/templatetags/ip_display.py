"""Template filters for dual-stack management address display and LabVault vs hardware URLs."""
from django import template

from connect.hardware_links import (
    hardware_login_url as _hardware_login_url,
    hardware_login_url_for_object,
    labvault_detail_path as _labvault_detail_path,
    mgmt_address_for,
    reverse_dns_hostname,
)
from connect.ip_addressing import is_valid_ip, normalize_ip
from connect.ip_addressing import bracket_host

register = template.Library()


@register.filter
def mgmt_display(obj):
    """Primary management address for labels (respects preferred_ip_version)."""
    if obj is None:
        return ''
    return getattr(obj, 'mgmt_display', None) or getattr(obj, 'connect_address', None) or getattr(
        obj, 'ip_address', '',
    ) or ''


@register.filter
def mgmt_label(obj):
    """Hostname plus management address when both differ."""
    if obj is None:
        return ''
    return getattr(obj, 'mgmt_label', None) or mgmt_display(obj)


@register.filter
def mgmt_https_url(addr):
    """HTTPS URL with bracketed IPv6 literal when needed."""
    a = (addr or '').strip()
    if not a:
        return ''
    return f'https://{bracket_host(a)}/'


@register.filter
def hardware_login_url(addr, resolved_hostname=''):
    """HTTPS hardware URL; optional second arg is pre-resolved PTR/FQDN for that IP."""
    return _hardware_login_url(addr, resolved_hostname=resolved_hostname or '')


@register.filter
def hardware_login_for(obj):
    """Hardware HTTPS URL for a device/chassis/SNMP row (PTR hostname when available)."""
    return hardware_login_url_for_object(obj)


@register.filter
def labvault_detail_path(obj):
    """In-app detail URL for a device/chassis object (``''`` when unknown)."""
    return _labvault_detail_path(obj)


@register.inclusion_tag('connect/_mgmt_address_links.html')
def mgmt_address_links(
    object=None,
    label=None,
    mgmt_addr='',
    fqdn='',
    internal_url='',
    show_internal=True,
    show_hardware=True,
    css_class='',
):
    """Primary label → LabVault; IP row → hardware HTTPS only."""
    resolved_hostname = (fqdn or '').strip()
    if object is not None:
        mgmt_addr = mgmt_addr or mgmt_address_for(object)
        if show_internal and not internal_url:
            internal_url = _labvault_detail_path(object)
        if not resolved_hostname:
            ptr_ip = (getattr(object, 'ip_address', None) or '').strip()
            if ptr_ip and is_valid_ip(ptr_ip):
                ptr_ip = normalize_ip(ptr_ip) or ptr_ip
            elif mgmt_addr and is_valid_ip(mgmt_addr):
                ptr_ip = normalize_ip(mgmt_addr) or mgmt_addr
            else:
                ptr_ip = ''
            if ptr_ip:
                resolved_hostname = reverse_dns_hostname(ptr_ip)
    primary = (label or '').strip()
    if not primary:
        primary = (
            (getattr(object, 'hostname', None) if object is not None else None)
            or mgmt_addr
            or (str(object) if object is not None else '')
        )
    hardware_href = ''
    if mgmt_addr and show_hardware:
        if not resolved_hostname and is_valid_ip(mgmt_addr):
            ptr_ip = normalize_ip(mgmt_addr) or mgmt_addr
            resolved_hostname = reverse_dns_hostname(ptr_ip)
        hardware_href = _hardware_login_url(mgmt_addr, resolved_hostname=resolved_hostname)
    return {
        'primary_label': primary,
        'mgmt_addr': mgmt_addr,
        'hardware_login_href': hardware_href,
        'internal_url': internal_url,
        'show_hardware': show_hardware and bool(mgmt_addr),
        'show_internal': show_internal and bool(internal_url),
        'css_class': css_class,
    }


