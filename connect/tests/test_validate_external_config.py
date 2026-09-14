from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings


class ValidateExternalConfigTests(SimpleTestCase):
    @override_settings(DEBUG=False, ALLOWED_HOSTS=["localhost"], SECRET_KEY="x" * 40)
    def test_ok_with_localhost_origin(self):
        env = {
            "DJANGO_DEBUG": "false",
            "LABVAULT_PUBLIC_ORIGIN": "http://127.0.0.1:8000",
            "LABVAULT_BOOTSTRAP_PASSWORD": "",
            "LABVAULT_DEMO_API_TOKEN": "",
            "LABVAULT_DEMO_DEFAULTS": "",
        }
        with patch.dict("os.environ", env, clear=False):
            call_command("validate_external_config", stdout=StringIO())

    @override_settings(DEBUG=False, ALLOWED_HOSTS=["*"], SECRET_KEY="x" * 40)
    def test_rejects_wildcard_hosts(self):
        with self.assertRaises(CommandError):
            call_command("validate_external_config", stdout=StringIO(), stderr=StringIO())

    @override_settings(DEBUG=False, ALLOWED_HOSTS=["localhost"], SECRET_KEY="x" * 40)
    def test_rejects_demo_password_unless_opted_in(self):
        with patch.dict("os.environ", {"LABVAULT_BOOTSTRAP_PASSWORD": "labvault!", "LABVAULT_DEMO_DEFAULTS": ""}, clear=False):
            with self.assertRaises(CommandError):
                call_command("validate_external_config", stdout=StringIO(), stderr=StringIO())
