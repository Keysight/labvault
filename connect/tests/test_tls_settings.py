import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TlsSettingsEnvTests(SimpleTestCase):
    def test_csrf_trusted_origin_from_public_hostname(self):
        env = os.environ.copy()
        env['DJANGO_SETTINGS_MODULE'] = 'connect.settings'
        env['LABVAULT_USE_TLS'] = 'true'
        env['LABVAULT_PUBLIC_HOSTNAME'] = 'labvaultvm.example.com'
        env.pop('LABVAULT_CSRF_TRUSTED_ORIGINS', None)
        snippet = (
            'from django.conf import settings; '
            'assert settings.CSRF_TRUSTED_ORIGINS[0] == '
            "'https://labvaultvm.example.com:9443', settings.CSRF_TRUSTED_ORIGINS; "
            "assert 'https://127.0.0.1:9443' in settings.CSRF_TRUSTED_ORIGINS"
        )
        proc = subprocess.run(
            [sys.executable, '-c', snippet],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=proc.stderr or proc.stdout,
        )

    def test_tls_is_on_by_default(self):
        env = os.environ.copy()
        env['DJANGO_SETTINGS_MODULE'] = 'connect.settings'
        env.pop('LABVAULT_USE_TLS', None)
        snippet = (
            'from django.conf import settings; '
            'assert settings.SESSION_COOKIE_SECURE is True; '
            'assert settings.LABVAULT_TLS_PORT == 9443'
        )
        proc = subprocess.run(
            [sys.executable, '-c', snippet],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
