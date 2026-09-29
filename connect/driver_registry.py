"""Unified device driver registry with plugin spikes (drop-in, entrypoint, REST).

Resolution order in :func:`resolve_driver` for ``Device.vendor_type`` (lower-cased):

1. Plugin manifest with ``rest_base_url`` while mode is ``rest``/``d1``/``all`` →
   :class:`connect.drivers.rest_adapter.RestAdapterDriver`.
2. Plugin manifest whose ``module``/``class_name`` imports a ``BaseDriver`` subclass.
3. Built-in ``connect.drivers.VENDOR_DRIVERS``.
4. :class:`connect.drivers.NullDriver`.

``LABVAULT_DRIVER_PLUGIN_MODE`` (default ``off``) selects discovery:
``dropin``/``default``/``b1`` scan ``LABVAULT_DRIVER_PATH`` (default
``/opt/labvault-drivers``) for ``*/manifest.yaml``; ``entrypoint``/``c1`` read the
``labvault.drivers`` entry-point group; ``rest``/``d1`` load only manifests with
``rest_base_url``; ``all`` combines entry points and REST. Discovery is cached per
process (``discover_plugin_manifests(force=True)`` to rescan). Importing a plugin
executes third-party code and may prepend its directory to ``sys.path``.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
from pathlib import Path
from typing import Any, Type

try:
    import yaml
except ImportError:  # pragma: no cover - optional until PyYAML installed
    yaml = None  # type: ignore

from connect.driver_manifest import DriverManifest
from connect.drivers.base import BaseDriver

logger = logging.getLogger(__name__)

_PLUGIN_CACHE: dict[str, DriverManifest] | None = None
_DRIVER_CLASS_CACHE: dict[str, Type[BaseDriver]] = {}


def plugin_mode() -> str:
    """Normalised ``LABVAULT_DRIVER_PLUGIN_MODE`` value (``'off'`` when unset)."""
    # Customer SKU default: off (built-in drivers only). Opt in with dropin/entrypoint/rest.
    return (os.environ.get('LABVAULT_DRIVER_PLUGIN_MODE', 'off') or 'off').strip().lower()


def _load_yaml_manifest(path: Path) -> DriverManifest | None:
    if yaml is None:
        logger.warning('PyYAML not installed; skipping driver manifest %s', path)
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning('driver manifest %s: %s', path, exc)
        return None
    if not isinstance(data, dict):
        return None
    m = DriverManifest.from_dict(data)
    m.plugin_dir = str(path.parent)
    return m if m.vendor_type else None


def _discover_dropin() -> dict[str, DriverManifest]:
    out: dict[str, DriverManifest] = {}
    root = os.environ.get('LABVAULT_DRIVER_PATH', '/opt/labvault-drivers').strip()
    if not root:
        return out
    base = Path(root)
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir()):
        manifest = child / 'manifest.yaml'
        if manifest.is_file():
            m = _load_yaml_manifest(manifest)
            if m:
                out[m.vendor_type] = m
    return out


def _discover_entrypoints() -> dict[str, DriverManifest]:
    out: dict[str, DriverManifest] = {}
    try:
        eps = importlib.metadata.entry_points()
        group = eps.select(group='labvault.drivers') if hasattr(eps, 'select') else eps.get('labvault.drivers', [])
    except Exception:
        return out
    for ep in group:
        vendor = ep.name.lower().strip()
        out[vendor] = DriverManifest(
            vendor_type=vendor,
            display_name=vendor,
            module=ep.module,
            class_name=ep.attr or 'Driver',
        )
    return out


def discover_plugin_manifests(*, force: bool = False) -> dict[str, DriverManifest]:
    """Return ``{vendor_type: DriverManifest}`` for the current plugin mode (cached)."""
    global _PLUGIN_CACHE
    if _PLUGIN_CACHE is not None and not force:
        return _PLUGIN_CACHE
    mode = plugin_mode()
    if mode in ('off', 'none', 'false', '0'):
        _PLUGIN_CACHE = {}
        return _PLUGIN_CACHE
    manifests: dict[str, DriverManifest] = {}
    if mode in ('dropin', 'default', 'b1'):
        manifests.update(_discover_dropin())
    if mode in ('entrypoint', 'c1', 'all'):
        manifests.update(_discover_entrypoints())
    if mode in ('rest', 'd1', 'all'):
        rest_dir = Path(os.environ.get('LABVAULT_DRIVER_PATH', '/opt/labvault-drivers'))
        if rest_dir.is_dir():
            for child in rest_dir.iterdir():
                mf = child / 'manifest.yaml'
                if mf.is_file():
                    m = _load_yaml_manifest(mf)
                    if m and m.rest_base_url:
                        manifests[m.vendor_type] = m
    _PLUGIN_CACHE = manifests
    return manifests


def _import_driver_class(manifest: DriverManifest) -> Type[BaseDriver] | None:
    key = manifest.vendor_type
    if key in _DRIVER_CLASS_CACHE:
        return _DRIVER_CLASS_CACHE[key]
    if not manifest.module or not manifest.class_name:
        return None
    try:
        import sys
        if manifest.plugin_dir and manifest.plugin_dir not in sys.path:
            sys.path.insert(0, manifest.plugin_dir)
        mod = importlib.import_module(manifest.module)
        cls = getattr(mod, manifest.class_name)
        if not issubclass(cls, BaseDriver):
            return None
        _DRIVER_CLASS_CACHE[key] = cls
        return cls
    except Exception as exc:
        logger.warning('import driver %s: %s', key, exc)
        return None


def list_vendor_choices() -> list[tuple[str, str]]:
    """Built-in ``VENDOR_CHOICES`` plus any plugin vendor types not already listed."""
    from connect.drivers import VENDOR_CHOICES

    choices = list(VENDOR_CHOICES)
    seen = {c[0] for c in choices}
    for m in discover_plugin_manifests().values():
        if m.vendor_type not in seen:
            choices.append((m.vendor_type, m.display_name))
            seen.add(m.vendor_type)
    return choices


def topology_node_type_for_vendor(vendor_type: str) -> str:
    """Topology node kind for a vendor (plugin manifest first, then built-in map)."""
    m = discover_plugin_manifests().get((vendor_type or '').lower().strip())
    if m:
        return m.topology_node_type
    _BUILTIN = {
        'arista': 'switch', 'sonic': 'switch', 'fortigate': 'firewall',
        'paloalto': 'firewall', 'keysight': 'chassis', 'ocs': 'ocs',
    }
    return _BUILTIN.get((vendor_type or '').lower().strip(), 'generic')


def resolve_driver(device) -> BaseDriver:
    """Resolve built-in or plugin driver for a Device."""
    from connect.drivers import VENDOR_DRIVERS, NullDriver
    from connect.drivers.rest_adapter import RestAdapterDriver

    vendor = (getattr(device, 'vendor_type', '') or '').lower().strip()
    manifests = discover_plugin_manifests()
    manifest = manifests.get(vendor)
    mode = plugin_mode()

    if manifest and mode in ('rest', 'd1', 'all') and manifest.rest_base_url:
        return RestAdapterDriver(device, manifest)

    if manifest:
        cls = _import_driver_class(manifest)
        if cls is not None:
            return cls(device)

    driver_class = VENDOR_DRIVERS.get(vendor)
    if driver_class is None:
        return NullDriver(device)
    return driver_class(device)


def registry_summary() -> dict[str, Any]:
    """Diagnostics payload: mode, built-in vendor keys, plugin manifests."""
    from connect.drivers import VENDOR_DRIVERS

    return {
        'mode': plugin_mode(),
        'builtin_vendors': sorted(VENDOR_DRIVERS.keys()),
        'plugins': [m.to_dict() for m in discover_plugin_manifests().values()],
    }
