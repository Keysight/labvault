"""Driver manifest schema for LabVault plugin spikes.

A manifest describes one out-of-tree vendor driver. It is loaded from
``<LABVAULT_DRIVER_PATH>/<plugin>/manifest.yaml`` (drop-in / REST modes) or
synthesised from a ``labvault.drivers`` entry point by
:mod:`connect.driver_registry`. Pure data — no I/O here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DriverManifest:
    """One plugin driver declaration.

    Fields:
        vendor_type: key matched against ``Device.vendor_type`` (lower-cased).
        display_name: label appended to vendor choices by ``list_vendor_choices``.
        module / class_name: import path of a ``BaseDriver`` subclass.
        topology_node_type: node kind for the topology map (``switch``, ``firewall``…).
        collector_hook: ``module:function`` called by ``metric_collectors`` for
            plugin vendors (function defaults to ``collect``).
        transport, commands, ui_panels: declarative metadata; no in-tree consumer.
        rest_base_url / rest_probe_path: REST sidecar settings (``RestAdapterDriver``).
        plugin_dir: directory of the manifest; prepended to ``sys.path`` on import.
    """

    vendor_type: str
    display_name: str
    module: str = ''
    class_name: str = ''
    topology_node_type: str = 'generic'
    transport: str = 'auto'
    commands: list[dict[str, str]] = field(default_factory=list)
    ui_panels: list[str] = field(default_factory=list)
    collector_hook: str = ''
  # REST adapter spike
    rest_base_url: str = ''
    rest_probe_path: str = '/probe'
    plugin_dir: str = ''

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'DriverManifest':
        """Build from parsed YAML; ``plugin_dir`` is set by the caller."""
        return cls(
            vendor_type=str(data.get('vendor_type', '')).lower().strip(),
            display_name=str(data.get('display_name') or data.get('vendor_type', '')),
            module=str(data.get('module', '')),
            class_name=str(data.get('class_name', '')),
            topology_node_type=str(data.get('topology_node_type', 'generic')),
            transport=str(data.get('transport', 'auto')),
            commands=list(data.get('commands') or []),
            ui_panels=list(data.get('ui_panels') or []),
            collector_hook=str(data.get('collector_hook', '')),
            rest_base_url=str(data.get('rest_base_url', '')),
            rest_probe_path=str(data.get('rest_probe_path', '/probe')),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialisable view used by ``registry_summary`` (omits ``plugin_dir``)."""
        return {
            'vendor_type': self.vendor_type,
            'display_name': self.display_name,
            'module': self.module,
            'class_name': self.class_name,
            'topology_node_type': self.topology_node_type,
            'transport': self.transport,
            'commands': self.commands,
            'ui_panels': self.ui_panels,
            'collector_hook': self.collector_hook,
            'rest_base_url': self.rest_base_url,
            'rest_probe_path': self.rest_probe_path,
        }
