# `resources/`

Example inputs shipped with LabVault. Every address is RFC 5737 TEST-NET (`192.0.2.x`) and
every credential is a placeholder — replace both before importing.

| File | Format | Used by |
|---|---|---|
| `ocs_photonic_site.json` | Site JSON v3: `common_tags`, `ocs_controller`, `ares_switches`, `arista_switches`, `keysight_chassis` | Default `--config` for `import_ocs_site_config` and `populate_ocs_lab_topology`; embedded in multibundles |
| `lab_topology_schema.json` | Layout JSON v1 (`label`, `site_json`, `layout.nodes[]`, `layout.links[]`) | Example DC builder (IPv4 preset) in the topology list page, `import_lab_topology --topo`; embedded in multibundles |
| `lab_topology_schema_v6.json` | Same layout format with IPv6 management addressing | Example DC builder `v6` preset (`lab_topology_io`) |
| `labvault_site_v1_example.yaml` | LabVault Site v1 YAML (`site_tags`, `ocs`, `switches`, `chassis`, `preset`) | Reference for `connect/lab_site_compiler.py` (onboard wizard `site_v1` mode) |
| [`examples/`](examples/README.md) | `labvault-full-export` v1 datasets | DATA → Import, `import_labvault_dataset`, `LABVAULT_RESTORE_DATASET` |

Formats and import pipelines:
[docs/development/subsystems/operations.md](../docs/development/subsystems/operations.md#dataset-and-bundle-importexport).
