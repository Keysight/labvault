"""Driver manifest schema for LabVault plugin spikes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DriverManifest:
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
