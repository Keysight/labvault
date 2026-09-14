"""
Build a LabTopology from a photonic OCS site JSON (e.g. resources/ocs_photonic_site.json).
Creates OCS + switch nodes and optic links to OCS per port_to_ocs_triplets. Does not add Keysight
chassis: those are not in the site file; use “Build from LLDP” to merge chassis/LLDP data.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction

from connect.models import Device, LabTopology, LabTopologyLink, LabTopologyNode
from connect.lab_topology_build import _grid, _infer_cable, _cable_choices, _color_for_cable


def _safe_key(s: str) -> str:
    t = re.sub(r"[^a-zA-Z0-9_]+", "_", (s or "").strip())
    t = t.strip("_") or "node"
    if len(t) > 48:
        t = t[:48]
    return t


@transaction.atomic
def build_lab_topology_from_ocs_json(
    path: str | Path,
    name: str,
    user,
    description: str = "",
) -> LabTopology:
    p = Path(path)
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "version" not in data:
        raise ValueError("JSON must include version")
    common = data.get("common_tags") or []
    ocs = data.get("ocs_controller")
    ares = data.get("ares_switches") or []
    arista = data.get("arista_switches") or []
    if not ocs and not ares and not arista:
        raise ValueError("No ocs_controller, ares_switches, or arista_switches in JSON")
    ocs_ip = (ocs or {}).get("ip", "").strip() if ocs else ""
    if not ocs_ip:
        raise ValueError("ocs_controller.ip is required for photonic site topology")

    tag_str = ",".join(t for t in common if t)

    topo = LabTopology.objects.create(
        name=name,
        description=description
        or f"Imported from {p.name} — static OCS port triplets; LLDP is separate.",
        source="import",
        tags=tag_str,
        created_by=user if user and getattr(user, "is_authenticated", False) else None,
    )

    # Order: OCS first, then ares, then arista — grid positions follow enumeration order
    spec: List[Dict[str, Any]] = []
    if ocs and ocs.get("ip"):
        spec.append(
            {
                "kind": "ocs",
                "ip": ocs["ip"].strip(),
                "name": ocs.get("name") or f"OCS-{ocs['ip']}",
                "node_key": "ocs_main",
            }
        )
    for b in ares:
        if not b or not b.get("ip"):
            continue
        spec.append(
            {
                "kind": "ares",
                "ip": b["ip"].strip(),
                "name": b.get("name") or b["ip"],
                "node_key": f"ares_{_safe_key(b.get('name') or b['ip'])}",
                "block": b,
            }
        )
    for b in arista:
        if not b or not b.get("ip"):
            continue
        spec.append(
            {
                "kind": "arista",
                "ip": b["ip"].strip(),
                "name": b.get("name") or b["ip"],
                "node_key": f"ar_{_safe_key(b.get('name') or b['ip'])}",
                "block": b,
            }
        )

    key_to_node: Dict[str, LabTopologyNode] = {}
    ocs_node: Optional[LabTopologyNode] = None

    for i, s in enumerate(spec):
        x, y = _grid(i, ncols=3, dx=280.0, dy=200.0)
        ip = s["ip"]
        dev = Device.objects.filter(ip_address=ip).first()
        ntype = "ocs" if s["kind"] == "ocs" else "switch"
        extra: Dict[str, Any] = {}
        if s.get("block"):
            b = s["block"]
            fm = b.get("fixed_mapping")
            if fm:
                extra["ocs_site_mapping"] = fm
        ltn = LabTopologyNode.objects.create(
            topology=topo,
            node_key=s["node_key"],
            device=dev,
            node_type=ntype,
            label=s["name"],
            x=x,
            y=y,
            extra=extra,
        )
        key_to_node[s["node_key"]] = ltn
        if s["kind"] == "ocs":
            ocs_node = ltn

    if ocs_node is None:
        raise ValueError("OCS node not created")

    for s in spec:
        if s["kind"] == "ocs":
            continue
        b = s.get("block") or {}
        fm = b.get("fixed_mapping") or {}
        triples = fm.get("port_to_ocs_triplets") or {}
        sw = key_to_node.get(s["node_key"])
        if not sw or not triples:
            continue
        for local_port, triplet_list in triples.items():
            for ocs_addr in triplet_list or []:
                ocs_s = (ocs_addr or "").strip()
                if not ocs_s:
                    continue
                ct = _infer_cable(str(local_port), ocs_s)
                LabTopologyLink.objects.create(
                    topology=topo,
                    node_a=sw,
                    port_a=str(local_port),
                    node_b=ocs_node,
                    port_b=ocs_s,
                    cable_type=_cable_choices(ct),
                    color=_color_for_cable(ct),
                    label="",
                )

    return topo
