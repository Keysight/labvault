"""
Arista Connect - Internal Validation Agent

This management command validates all features of the Arista Connect application:
- URL routing configuration
- Model integrity
- View accessibility
- Template rendering
- Form validation
- Database operations
- API endpoints

Usage:
    python manage.py validate_app
    python manage.py validate_app --verbose
"""

import sys
from io import StringIO
from django.core.management.base import BaseCommand
from django.test import RequestFactory, TestCase
from django.test.client import Client
from django.contrib.auth.models import User
from django.urls import reverse, resolve, NoReverseMatch
from django.db import connection
from connect.models import Device, AuditLog, DeviceSnapshot
from connect.forms import ConnectionForm, CommandForm, DeviceImportForm


class Command(BaseCommand):
    help = 'Validates all Arista Connect features and reports issues'

    def add_arguments(self, parser):
        parser.add_argument('--verbose', action='store_true', help='Show detailed output')

    def handle(self, *args, **options):
        self.verbose = options.get('verbose', False)
        self.passed = 0
        self.failed = 0
        self.warnings = 0
        self.errors_list = []

        self.stdout.write(self.style.MIGRATE_HEADING(
            '\n========================================\n'
            '  Arista Connect Validation Agent\n'
            '========================================\n'
        ))

        self._validate_urls()
        self._validate_models()
        self._validate_forms()
        self._validate_views()
        self._validate_database()
        self._validate_templates()
        self._validate_api()

        self._print_summary()

    def _check(self, name, condition, error_msg=''):
        if condition:
            self.passed += 1
            if self.verbose:
                self.stdout.write(self.style.SUCCESS(f'  PASS: {name}'))
        else:
            self.failed += 1
            self.errors_list.append(f'{name}: {error_msg}')
            self.stdout.write(self.style.ERROR(f'  FAIL: {name} - {error_msg}'))

    def _warn(self, name, msg):
        self.warnings += 1
        self.stdout.write(self.style.WARNING(f'  WARN: {name} - {msg}'))

    def _section(self, title):
        self.stdout.write(self.style.MIGRATE_HEADING(f'\n--- {title} ---'))

    def _validate_urls(self):
        self._section('URL Routing')

        url_names = [
            'login', 'logout', 'home', 'dashboard',
            'add_device', 'import_devices', 'export_devices',
            'audit_log', 'api_devices',
        ]
        for name in url_names:
            try:
                url = reverse(name)
                self._check(f'URL "{name}" resolves', True)
            except NoReverseMatch:
                self._check(f'URL "{name}" resolves', False, 'NoReverseMatch')

        # URLs with args
        url_names_with_args = [
            ('device_detail', [1]),
            ('device_health', [1]),
            ('device_routing', [1]),
            ('device_vlans', [1]),
            ('device_config', [1]),
            ('delete_device', [1]),
            ('api_device_detail', [1]),
            ('api_device_health', [1]),
            ('api_device_ocs_patch', [1]),
        ]
        for name, args in url_names_with_args:
            try:
                url = reverse(name, args=args)
                self._check(f'URL "{name}" resolves with args', True)
            except NoReverseMatch:
                self._check(f'URL "{name}" resolves with args', False, 'NoReverseMatch')

    def _validate_models(self):
        self._section('Models')

        # Device model
        self._check('Device model exists', hasattr(Device, '_meta'))
        expected_fields = ['ip_address', 'username', 'password', 'version',
                           'serial_number', 'model_name', 'hostname', 'status',
                           'mac_address', 'uptime', 'tags', 'notes', 'last_seen',
                           'created_at', 'updated_at']
        for field in expected_fields:
            has_field = any(f.name == field for f in Device._meta.get_fields())
            self._check(f'Device has field "{field}"', has_field, f'Missing field')

        # AuditLog model
        self._check('AuditLog model exists', hasattr(AuditLog, '_meta'))

        # DeviceSnapshot model
        self._check('DeviceSnapshot model exists', hasattr(DeviceSnapshot, '_meta'))

        # Test model methods
        d = Device(ip_address='10.0.0.1', hostname='test-switch')
        self._check('Device __str__ works', str(d) == 'test-switch')
        self._check('Device tag_list property works', d.tag_list == [])
        d.tags = 'spine, leaf'
        self._check('Device tag_list parsing', d.tag_list == ['spine', 'leaf'])
        d.uptime = 90061
        self._check('Device uptime_display', '1d' in d.uptime_display)

    def _validate_forms(self):
        self._section('Forms')

        # ConnectionForm
        form = ConnectionForm(data={'ip_address': '10.0.0.1', 'username': 'admin', 'password': 'admin'})
        self._check('ConnectionForm valid with correct data', form.is_valid())

        form = ConnectionForm(data={})
        self._check('ConnectionForm rejects empty data', not form.is_valid())

        # CommandForm
        form = CommandForm(data={'command': 'show version'})
        self._check('CommandForm valid with command', form.is_valid())

        form = CommandForm(data={})
        self._check('CommandForm rejects empty', not form.is_valid())

    def _validate_views(self):
        self._section('Views (HTTP)')

        client = Client()

        # Create test user
        user, created = User.objects.get_or_create(
            username='_validate_test_user',
            defaults={'is_active': True}
        )
        if created:
            user.set_password('testpass123')
            user.save()

        try:
            # Unauthenticated access should redirect to login
            response = client.get(reverse('dashboard'))
            self._check('Dashboard redirects unauthenticated', response.status_code == 302)

            response = client.get(reverse('login'))
            self._check('Login page accessible', response.status_code == 200)

            # Login
            logged_in = client.login(username='_validate_test_user', password='testpass123')
            self._check('Test user can login', logged_in)

            if logged_in:
                response = client.get(reverse('dashboard'))
                self._check('Dashboard accessible after login', response.status_code == 200)

                response = client.get(reverse('add_device'))
                self._check('Add device page accessible', response.status_code == 200)

                response = client.get(reverse('import_devices'))
                self._check('Import devices page accessible', response.status_code == 200)

                response = client.get(reverse('audit_log'))
                self._check('Audit log page accessible', response.status_code == 200)

                response = client.get(reverse('export_devices'))
                self._check('Export CSV works', response.status_code == 200)
                self._check('Export returns CSV', response.get('Content-Type') == 'text/csv')

                # API endpoints
                response = client.get(reverse('api_devices'))
                self._check('API devices endpoint works', response.status_code == 200)

                # Add a test device
                response = client.post(reverse('add_device'), {
                    'ip_address': '192.168.255.254',
                    'username': 'test',
                    'password': 'test',
                    'tags': 'test',
                    'notes': 'validation test device',
                })
                self._check('Add device form POST works', response.status_code in [200, 302])

                # Check it was created
                test_device = Device.objects.filter(ip_address='192.168.255.254').first()
                self._check('Test device created in DB', test_device is not None)

                if test_device:
                    # Device detail
                    response = client.get(reverse('device_detail', args=[test_device.id]))
                    self._check('Device detail page renders', response.status_code == 200)

                    # Device routing
                    response = client.get(reverse('device_routing', args=[test_device.id]))
                    self._check('Device routing page renders', response.status_code == 200)

                    # Device VLANs
                    response = client.get(reverse('device_vlans', args=[test_device.id]))
                    self._check('Device VLANs page renders', response.status_code == 200)

                    # Device config
                    response = client.get(reverse('device_config', args=[test_device.id]))
                    self._check('Device config page renders', response.status_code == 200)

                    # API device detail
                    response = client.get(reverse('api_device_detail', args=[test_device.id]))
                    self._check('API device detail works', response.status_code == 200)

                    # Delete device
                    response = client.post(reverse('delete_device', args=[test_device.id]))
                    self._check('Delete device works', response.status_code == 302)

                    # Verify deletion
                    self._check('Device deleted from DB',
                               not Device.objects.filter(ip_address='192.168.255.254').exists())

                # Audit log should have entries now
                logs_count = AuditLog.objects.filter(user=user).count()
                self._check('Audit log entries created', logs_count > 0)

        finally:
            # Cleanup
            Device.objects.filter(ip_address='192.168.255.254').delete()
            AuditLog.objects.filter(user=user).delete()
            user.delete()

    def _validate_database(self):
        self._section('Database')

        # Check tables exist
        tables = connection.introspection.table_names()
        self._check('connect_device table exists', 'connect_device' in tables)
        self._check('connect_auditlog table exists', 'connect_auditlog' in tables)
        self._check('connect_devicesnapshot table exists', 'connect_devicesnapshot' in tables)

    def _validate_templates(self):
        self._section('Templates')

        from django.template.loader import get_template
        templates_to_check = [
            'connect/base.html',
            'connect/login.html',
            'connect/dashboard.html',
            'connect/device_detail.html',
            'connect/add_device.html',
            'connect/device_routing.html',
            'connect/device_vlans.html',
            'connect/device_config.html',
            'connect/audit_log.html',
            'connect/import_devices.html',
        ]
        for tmpl in templates_to_check:
            try:
                get_template(tmpl)
                self._check(f'Template "{tmpl}" loads', True)
            except Exception as e:
                self._check(f'Template "{tmpl}" loads', False, str(e))

    def _validate_api(self):
        self._section('API Endpoints')

        client = Client()
        user, created = User.objects.get_or_create(
            username='_validate_api_user',
            defaults={'is_active': True}
        )
        if created:
            user.set_password('apipass123')
            user.save()

        try:
            client.login(username='_validate_api_user', password='apipass123')

            response = client.get(reverse('api_devices'))
            self._check('API /api/devices/ returns JSON', 'application/json' in response.get('Content-Type', ''))

            import json
            data = json.loads(response.content)
            self._check('API devices response has "devices" key', 'devices' in data)

        finally:
            user.delete()

    def _print_summary(self):
        self.stdout.write(self.style.MIGRATE_HEADING(
            '\n========================================\n'
            '  Validation Summary\n'
            '========================================'
        ))
        self.stdout.write(self.style.SUCCESS(f'  Passed:   {self.passed}'))
        if self.failed:
            self.stdout.write(self.style.ERROR(f'  Failed:   {self.failed}'))
        else:
            self.stdout.write(f'  Failed:   {self.failed}')
        if self.warnings:
            self.stdout.write(self.style.WARNING(f'  Warnings: {self.warnings}'))
        self.stdout.write('')

        if self.errors_list:
            self.stdout.write(self.style.ERROR('\nFailed checks:'))
            for err in self.errors_list:
                self.stdout.write(self.style.ERROR(f'  - {err}'))

        if self.failed == 0:
            self.stdout.write(self.style.SUCCESS(
                '\n  All validations passed! The application is healthy.\n'
            ))
        else:
            self.stdout.write(self.style.ERROR(
                f'\n  {self.failed} validation(s) failed. Please fix the issues above.\n'
            ))
            sys.exit(1)
