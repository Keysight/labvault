"""About page helpers — product identity + installed library versions."""
from __future__ import annotations

import platform
import sys
from importlib import metadata
from typing import Any

_PACKAGE_NAMES = (
    'Django',
    'gunicorn',
    'requests',
    'paramiko',
    'psycopg2-binary',
    'psycopg2',
    'PyYAML',
    'python-dotenv',
    'psutil',
    'redfish',
    'dj-database-url',
    'jsonrpclib-pelix',
    'django-sslserver',
    'django-auth-ldap',
)


def _pkg_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def collect_runtime_info() -> dict[str, Any]:
    django_ver = _pkg_version('Django') or 'unknown'
    return {
        'python': platform.python_version(),
        'python_impl': platform.python_implementation(),
        'django': django_ver,
        'platform': platform.platform(),
        'executable': sys.executable,
    }


def collect_library_versions() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for name in _PACKAGE_NAMES:
        key = name.lower().replace('_', '-')
        if key in seen:
            continue
        ver = _pkg_version(name)
        if ver is None:
            continue
        seen.add(key)
        rows.append({'name': name, 'version': ver})
    return rows


def about_context() -> dict[str, Any]:
    """Context for connect/about.html — LabVault contributors attribution."""
    return {
        'product_name': 'LabVault',
        'product_attribution': 'LabVault contributors',
        'design_credit_name': 'LabVault contributors',
        'design_credit_email': '',
        'runtime': collect_runtime_info(),
        'libraries': collect_library_versions(),
    }
