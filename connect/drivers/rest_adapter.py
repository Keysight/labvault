"""REST sidecar driver adapter (spike D1).

Selected by :func:`connect.driver_registry.resolve_driver` only when
``LABVAULT_DRIVER_PLUGIN_MODE`` is ``rest`` / ``d1`` / ``all`` and the vendor's
``manifest.yaml`` sets ``rest_base_url``. Each call is a JSON ``POST`` (timeout
15 s) to the operator-hosted service:

- ``<rest_probe_path>`` (default ``/probe``) — body ``{ip, username, vendor_type}``;
  response ``status`` / ``probe`` field becomes the probe result.
- ``/health``, ``/interfaces`` — body ``{ip, username}``; JSON response is returned
  verbatim as ``DriverResult.data``.
- ``/command`` — body ``{ip, command}``.

The device password is not forwarded; the sidecar owns its own credentials.
Other ``BaseDriver`` methods are not overridden (core ones raise
``NotImplementedError``).
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urljoin

import requests

from connect.driver_manifest import DriverManifest
from connect.drivers.base import BaseDriver, DriverResult

logger = logging.getLogger(__name__)


class RestAdapterDriver(BaseDriver):
    """Proxy device operations to a customer HTTP driver service."""

    VENDOR_NAME = 'rest_adapter'

    def __init__(self, device, manifest: DriverManifest):
        super().__init__(device)
        self._manifest = manifest
        self._base = (manifest.rest_base_url or '').rstrip('/')

    def _url(self, path: str) -> str:
        return urljoin(self._base + '/', path.lstrip('/'))

    def _post(self, path: str, payload: dict[str, Any]) -> DriverResult:
        """POST JSON to the sidecar; non-JSON bodies come back as ``{'raw': ...}``."""
        if not self._base:
            return DriverResult(False, error='rest_base_url not configured')
        try:
            resp = requests.post(
                self._url(path),
                json=payload,
                timeout=15,
                headers={'Accept': 'application/json'},
            )
            if resp.status_code >= 400:
                return DriverResult(False, error=f'HTTP {resp.status_code}: {resp.text[:300]}')
            try:
                data = resp.json()
            except json.JSONDecodeError:
                data = {'raw': resp.text[:500]}
            return DriverResult(True, data=data)
        except requests.RequestException as exc:
            return DriverResult(False, error=str(exc))

    def probe(self) -> str:
        result = self._post(self._manifest.rest_probe_path, {
            'ip': self.ip,
            'username': self.username,
            'vendor_type': self.device.vendor_type,
        })
        if not result.success:
            return 'unreachable'
        data = result.data if isinstance(result.data, dict) else {}
        return str(data.get('status') or data.get('probe') or 'ok')

    def get_health(self) -> DriverResult:
        return self._post('/health', {'ip': self.ip, 'username': self.username})

    def get_interfaces(self) -> DriverResult:
        return self._post('/interfaces', {'ip': self.ip, 'username': self.username})

    def execute_command(self, command: str) -> DriverResult:
        return self._post('/command', {'ip': self.ip, 'command': command})
