"""Optional site fabric-path helpers.

Customer SKU ships with an empty map. Load a site file at runtime or call
``set_fabric_map`` from tests. Do not bake a specific lab's IPs here.

The map is ``{mgmt_ip: {chassis_id, node_key, path: 'ocs'|'dac_direct',
ocs_connected, ocs_ip, ocs_blade, peer_ip, peer_label}}``. Module globals are
rebound by ``set_fabric_map``; import the module (not the names) to see updates.
``ocs_fixed_mapping_is_active`` is used by ``ocs_helpers`` to skip site
``fixed_mapping`` blocks that are DAC-direct or pending a physical patch.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

OCS_CONTROLLER_IP = ""
OCS_CONTROLLER_LABEL = "OCS"
ARESONE_FABRIC_BY_IP: Dict[str, Dict[str, Any]] = {}
OCS_CHASSIS_IPS: frozenset = frozenset()
DAC_DIRECT_CHASSIS_IPS: frozenset = frozenset()


def set_fabric_map(
    mapping: Optional[Dict[str, Dict[str, Any]]] = None,
    *,
    ocs_ip: str = "",
    ocs_label: str = "OCS",
) -> None:
    """Replace the in-memory fabric map (tests / optional site import)."""
    global ARESONE_FABRIC_BY_IP, OCS_CONTROLLER_IP, OCS_CONTROLLER_LABEL
    global OCS_CHASSIS_IPS, DAC_DIRECT_CHASSIS_IPS
    OCS_CONTROLLER_IP = ocs_ip or ""
    OCS_CONTROLLER_LABEL = ocs_label or "OCS"
    ARESONE_FABRIC_BY_IP = dict(mapping or {})
    OCS_CHASSIS_IPS = frozenset(
        ip for ip, meta in ARESONE_FABRIC_BY_IP.items() if meta.get("ocs_connected")
    )
    DAC_DIRECT_CHASSIS_IPS = frozenset(
        ip for ip, meta in ARESONE_FABRIC_BY_IP.items() if meta.get("path") == "dac_direct"
    )


def lookup_aresone_fabric(mgmt_ip: str) -> Optional[Dict[str, Any]]:
    """Copy of the fabric-map entry for one chassis management IP, or ``None``."""
    ip = (mgmt_ip or "").strip()
    meta = ARESONE_FABRIC_BY_IP.get(ip)
    return dict(meta) if meta else None


def fabric_path_label(meta: Dict[str, Any]) -> str:
    """Short UI label such as ``OCS → <label>`` or ``DAC → <peer>``."""
    if meta.get("path") == "ocs":
        return f"OCS → {meta.get('ocs_label', OCS_CONTROLLER_LABEL)}"
    if meta.get("path") == "dac_direct":
        return f"DAC → {meta.get('peer_label', 'Arista')}"
    return meta.get("path") or "unknown"


def _fabric_summary_rows() -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ocs_rows: List[Dict[str, Any]] = []
    dac_rows: List[Dict[str, Any]] = []
    for ip, meta in sorted(ARESONE_FABRIC_BY_IP.items(), key=lambda x: x[1].get("chassis_id", "")):
        row = {
            "mgmt_ip": ip,
            "chassis_id": meta.get("chassis_id"),
            "node_key": meta.get("node_key"),
            "path": meta.get("path"),
            "path_label": fabric_path_label(meta),
        }
        if meta.get("ocs_connected"):
            row["ocs_ip"] = meta.get("ocs_ip")
            row["ocs_blade"] = meta.get("ocs_blade")
            ocs_rows.append(row)
        else:
            row["peer_ip"] = meta.get("peer_ip")
            row["peer_label"] = meta.get("peer_label")
            dac_rows.append(row)
    return ocs_rows, dac_rows


def build_site_fabric_summary() -> Dict[str, Any]:
    """Site-wide OCS vs DAC-direct chassis summary (the ``notes`` text is static example copy)."""
    ocs_rows, dac_rows = _fabric_summary_rows()
    return {
        "scope": "site",
        "scope_label": "Configured site fabric",
        "description": "OCS-patched and DAC-direct chassis from the loaded site map.",
        "ocs_controller_ip": OCS_CONTROLLER_IP,
        "ocs_chassis_count": len(ocs_rows),
        "dac_direct_chassis_count": len(dac_rows),
        "ocs_chassis": ocs_rows,
        "dac_direct_chassis": dac_rows,
        "has_ocs_chassis": bool(ocs_rows),
        "has_dac_direct_chassis": bool(dac_rows),
        "notes": [
            "AresONE M01–M04 use direct DAC to Arista (no OCS patch).",
            "AresONE M05–M08 use optical ports into OCS S320.",
            "Arista 1–4 spine switches uplink to OCS on ports 9–32.",
        ],
    }


def build_topology_fabric_summary(
    *,
    chassis_mgmt_ips: Optional[List[str]] = None,
    has_ocs_node: bool = False,
) -> Dict[str, Any]:
    """Site fabric model scoped to chassis actually present in one LabTopology."""
    site_ocs, site_dac = _fabric_summary_rows()
    ips = {ip.strip() for ip in (chassis_mgmt_ips or []) if (ip or "").strip()}
    if not ips:
        return build_site_fabric_summary()

    ocs_rows = [r for r in site_ocs if r["mgmt_ip"] in ips]
    dac_rows = [r for r in site_dac if r["mgmt_ip"] in ips]
    dac_ids = [r["chassis_id"] for r in dac_rows if r.get("chassis_id")]
    ocs_ids = [r["chassis_id"] for r in ocs_rows if r.get("chassis_id")]

    notes: List[str] = []
    if dac_rows:
        notes.append(
            f"{', '.join(dac_ids)}: direct DAC to Arista — not patched to OCS."
        )
    if ocs_rows:
        notes.append(
            f"{', '.join(ocs_ids)}: optical ports patched to OCS ({OCS_CONTROLLER_IP or 'configured controller'})."
        )
    if has_ocs_node and dac_rows and not ocs_rows:
        notes.append(
            "OCS controller is in this topology for Arista spine uplinks only; "
            "listed AresONE chassis use DAC-direct paths."
        )
    elif not has_ocs_node and ocs_rows:
        notes.append(
            "OCS chassis listed but no OCS node in topology — import/sync may be stale."
        )
    elif not has_ocs_node and dac_rows and not ocs_rows:
        notes.append("DAC-direct staging — no OCS cross-connects in this topology.")

    if dac_rows and ocs_rows:
        scope_label = f"Mixed · {len(dac_rows)} DAC + {len(ocs_rows)} OCS chassis"
        description = (
            f"{', '.join(dac_ids)} → Arista DAC · "
            f"{', '.join(ocs_ids)} → OCS S320"
            + (" · Arista spines → OCS" if has_ocs_node else "")
        )
    elif ocs_rows:
        scope_label = f"OCS-patched · {len(ocs_rows)} chassis"
        description = (
            f"{', '.join(ocs_ids)} → OCS S320"
            + (" · Arista spines → OCS" if has_ocs_node else "")
        )
    elif dac_rows:
        scope_label = f"DAC-direct · {len(dac_rows)} chassis"
        description = f"{', '.join(dac_ids)} → Arista DAC (no OCS chassis in topology)"
    else:
        scope_label = "No mapped AresONE chassis"
        description = "Chassis in topology are outside the loaded site fabric map."

    return {
        "scope": "topology",
        "scope_label": scope_label,
        "description": description,
        "ocs_controller_ip": OCS_CONTROLLER_IP if has_ocs_node else "",
        "ocs_chassis_count": len(ocs_rows),
        "dac_direct_chassis_count": len(dac_rows),
        "ocs_chassis": ocs_rows,
        "dac_direct_chassis": dac_rows,
        "has_ocs_chassis": bool(ocs_rows),
        "has_dac_direct_chassis": bool(dac_rows),
        "has_ocs_node": has_ocs_node,
        "notes": notes,
    }


def aggregate_fabric_path_ports(chassis_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Count live ports by canonical fabric_path (not raw port role tags)."""
    buckets: Dict[str, Dict[str, int]] = {
        "dac_direct": {"up": 0, "total": 0},
        "ocs": {"up": 0, "total": 0},
    }
    for chassis in chassis_rows or []:
        path = chassis.get("fabric_path")
        if not path:
            path = "ocs" if chassis.get("ocs_connected") else "dac_direct"
        if path not in buckets:
            buckets[path] = {"up": 0, "total": 0}
        for slot in chassis.get("slots") or []:
            buckets[path]["up"] += int(slot.get("ports_up") or 0)
            buckets[path]["total"] += int(slot.get("ports_total") or 0)
    return buckets


def ocs_fixed_mapping_is_active(fixed_mapping: Optional[Dict[str, Any]]) -> bool:
    """True when site JSON fixed_mapping should contribute OCS triplets."""
    fm = fixed_mapping or {}
    if fm.get("dac_direct") or fm.get("_path") == "dac_to_arista":
        return False
    if fm.get("_patch_status") == "pending_physical_patch":
        return False
    return bool(fm.get("port_to_ocs_triplets"))
