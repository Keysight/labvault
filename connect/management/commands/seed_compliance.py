"""
Seed pre-built compliance rules for all vendors.
Usage: python manage.py seed_compliance
"""
from django.core.management.base import BaseCommand
from connect.models import ComplianceRule


COMPLIANCE_TEMPLATES = [
    # NTP
    {'name': 'NTP Server Configured (Arista)', 'category': 'ntp', 'vendor_type': 'arista',
     'pattern': r'ntp server', 'should_exist': True, 'severity': 'warning',
     'description': 'Ensure at least one NTP server is configured.'},
    {'name': 'NTP Server Configured (SONiC)', 'category': 'ntp', 'vendor_type': 'sonic',
     'pattern': r'NTP_SERVER', 'should_exist': True, 'severity': 'warning',
     'description': 'Ensure NTP is configured in SONiC config.'},
    # AAA
    {'name': 'AAA Authentication (Arista)', 'category': 'aaa', 'vendor_type': 'arista',
     'pattern': r'aaa authentication', 'should_exist': True, 'severity': 'critical',
     'description': 'Ensure AAA authentication is configured.'},
    {'name': 'RADIUS/TACACS Configured (Arista)', 'category': 'aaa', 'vendor_type': 'arista',
     'pattern': r'(radius-server|tacacs-server) host', 'should_exist': True, 'severity': 'warning',
     'description': 'Ensure RADIUS or TACACS+ server is configured.'},
    # Logging
    {'name': 'Syslog Server (Arista)', 'category': 'logging', 'vendor_type': 'arista',
     'pattern': r'logging host', 'should_exist': True, 'severity': 'warning',
     'description': 'Ensure syslog server is configured.'},
    {'name': 'Logging Enabled (FortiGate)', 'category': 'logging', 'vendor_type': 'fortigate',
     'pattern': r'set logtraffic all', 'should_exist': True, 'severity': 'warning',
     'description': 'Ensure traffic logging is enabled on policies.'},
    # SNMP
    {'name': 'SNMP Community (Arista)', 'category': 'snmp', 'vendor_type': 'arista',
     'pattern': r'snmp-server community', 'should_exist': True, 'severity': 'info',
     'description': 'Ensure SNMP community is configured for monitoring.'},
    {'name': 'No Default SNMP Community', 'category': 'snmp', 'vendor_type': 'arista',
     'pattern': r'snmp-server community (public|private)', 'should_exist': False, 'severity': 'critical',
     'description': 'Default SNMP communities should not be used.'},
    # Security
    {'name': 'SSH Enabled (Arista)', 'category': 'security', 'vendor_type': 'arista',
     'pattern': r'management ssh', 'should_exist': True, 'severity': 'info',
     'description': 'Ensure SSH management is enabled.'},
    {'name': 'No Telnet (Arista)', 'category': 'security', 'vendor_type': 'arista',
     'pattern': r'management telnet', 'should_exist': False, 'severity': 'critical',
     'description': 'Telnet should be disabled for security.'},
    {'name': 'Password Encryption (Arista)', 'category': 'security', 'vendor_type': 'arista',
     'pattern': r'service password-encryption', 'should_exist': True, 'severity': 'warning',
     'description': 'Passwords should be encrypted in config.'},
    # Management
    {'name': 'Hostname Set (Arista)', 'category': 'management', 'vendor_type': 'arista',
     'pattern': r'^hostname ', 'should_exist': True, 'severity': 'info',
     'description': 'Ensure hostname is configured.'},
    {'name': 'DNS Configured (Arista)', 'category': 'management', 'vendor_type': 'arista',
     'pattern': r'ip name-server', 'should_exist': True, 'severity': 'info',
     'description': 'Ensure DNS name server is configured.'},
]


class Command(BaseCommand):
    help = 'Seed pre-built compliance rule templates'

    def handle(self, *args, **options):
        created = 0
        for tmpl in COMPLIANCE_TEMPLATES:
            _, was_created = ComplianceRule.objects.get_or_create(
                name=tmpl['name'],
                defaults={
                    'category': tmpl['category'],
                    'vendor_type': tmpl['vendor_type'],
                    'pattern': tmpl['pattern'],
                    'should_exist': tmpl['should_exist'],
                    'severity': tmpl['severity'],
                    'description': tmpl['description'],
                }
            )
            if was_created:
                created += 1
        self.stdout.write(self.style.SUCCESS(f'Created {created} compliance rules ({len(COMPLIANCE_TEMPLATES)} total templates)'))
