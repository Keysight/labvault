"""AresONE 800GE resource-group fanout planning (pure functions, no I/O).

Each 800GE-8P card exposes 8 resource groups (RGs). Fanout mode is fixed per
card and determines how each RG is split:

  8×8×100G  → 8 RGs × 8 ports × 100G  (64 ports/card)
  8×4×200G  → 8 RGs × 4 ports × 200G  (32 ports/card)
  8×16×50G  → 8 RGs × 16 ports × 50G (128 ports/card)
  8×1×800G  → 8 RGs × 1 port × 800G   (8 ports/card)

B2B loopback needs two ports at the same line rate → ceil(port_count / 2) pairs.

Capacity checks take a *fabric detail* dict: ``{chassis: [{family, slots:
[{card_type, ports_up, ports_total}]}], ocs_paths: {total, active},
nodes_by_kind}``. Physical up/total counts are scaled to logical ports for the
requested fanout mode. Not imported by any view in this tree (tests only).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


ARESONE_RG_PER_CARD = 8
ARESONE_CARD_DEFAULT = "800GE-8P-OSFP-M+NRZ+ROCEV2"


@dataclass(frozen=True)
class FanoutMode:
    mode_id: str
    ports_per_rg: int
    line_rate_gbps: int
    label: str

    @property
    def max_ports_per_card(self) -> int:
        return ARESONE_RG_PER_CARD * self.ports_per_rg


FANOUT_MODES: tuple[FanoutMode, ...] = (
    FanoutMode("8x1x800G", 1, 800, "8×1×800G"),
    FanoutMode("8x4x200G", 4, 200, "8×4×200G"),
    FanoutMode("8x8x100G", 8, 100, "8×8×100G"),
    FanoutMode("8x16x50G", 16, 50, "8×16×50G"),
)

_MODE_BY_GBPS = {m.line_rate_gbps: m for m in FANOUT_MODES}


def fanout_modes_for_api() -> List[Dict[str, Any]]:
    """JSON-friendly list of supported fanout modes."""
    return [
        {
            "id": m.mode_id,
            "label": m.label,
            "ports_per_rg": m.ports_per_rg,
            "line_rate_gbps": m.line_rate_gbps,
            "max_ports_per_card": m.max_ports_per_card,
            "rg_per_card": ARESONE_RG_PER_CARD,
        }
        for m in FANOUT_MODES
    ]


def parse_line_rate_gbps(line_rate: str) -> Optional[int]:
    """Parse ``"400G"`` / ``"400"`` / ``"400Gbps"`` → 400; ``None`` if unparseable."""
    s = (line_rate or "").strip().upper().replace(" ", "")
    if not s:
        return None
    m = re.match(r"^(\d+)(?:G|GBPS)?$", s)
    if not m:
        return None
    return int(m.group(1))


def mode_for_line_rate(line_rate_gbps: int) -> Optional[FanoutMode]:
    """Fanout mode whose per-port rate equals ``line_rate_gbps``."""
    return _MODE_BY_GBPS.get(int(line_rate_gbps))


def is_aresone_card_type(card_type: str) -> bool:
    """True for 800GE / AresONE card type strings."""
    ct = (card_type or "").upper()
    return "800GE" in ct or "ARESONE" in ct


def plan_aresone_requirement(
    port_count: int,
    line_rate: str,
    *,
    card_type: str = ARESONE_CARD_DEFAULT,
) -> Dict[str, Any]:
    """Compute RG / pair needs for an AresONE 800GE reservation request."""
    ports = max(1, int(port_count or 1))
    gbps = parse_line_rate_gbps(line_rate)
    if gbps is None:
        return {
            "ok": False,
            "applies": True,
            "error": "line_rate_required",
            "message": "Select a line rate (50G–800G) for AresONE fanout planning.",
        }

    mode = mode_for_line_rate(gbps)
    if mode is None:
        supported = ", ".join(f"{m.line_rate_gbps}G ({m.label})" for m in FANOUT_MODES)
        return {
            "ok": False,
            "applies": True,
            "error": "unsupported_line_rate",
            "message": f"No AresONE fanout mode for {gbps}G. Supported: {supported}.",
            "port_count": ports,
            "line_rate_gbps": gbps,
        }

    pairs_needed = max(1, math.ceil(ports / 2))
    rgs_needed = max(1, math.ceil(ports / mode.ports_per_rg))
    cards_needed = max(1, math.ceil(rgs_needed / ARESONE_RG_PER_CARD))
    max_on_one_card = mode.max_ports_per_card

    return {
        "ok": True,
        "applies": True,
        "card_type": card_type,
        "fanout_mode": mode.mode_id,
        "fanout_label": mode.label,
        "line_rate_gbps": gbps,
        "port_count": ports,
        "pairs_needed": pairs_needed,
        "ports_per_rg": mode.ports_per_rg,
        "rg_per_card": ARESONE_RG_PER_CARD,
        "rgs_needed": rgs_needed,
        "cards_needed": cards_needed,
        "max_ports_per_card": max_on_one_card,
        "message": (
            f"{ports}×{gbps}G on {mode.label} → {rgs_needed} RG(s), "
            f"{pairs_needed} B2B pair(s), ≥{cards_needed} AresONE card(s)"
        ),
    }


def _chassis_is_aresone(ch: Dict[str, Any]) -> bool:
    fam = (ch.get("family") or "").lower()
    if fam and "aresone" not in fam and "ares" not in fam:
        return False
    return True


def _logical_pool_from_slot(
    slot: Dict[str, Any],
    mode: FanoutMode,
) -> tuple[int, int]:
    """Map physical link telemetry on an 800GE card to logical ports at *mode* fanout."""
    ct = (slot.get("card_type") or "").upper()
    if not is_aresone_card_type(ct):
        return 0, 0
    logical_total = mode.max_ports_per_card
    pu = int(slot.get("ports_up") or 0)
    pt = int(slot.get("ports_total") or 0)
    if pt <= 0:
        pt = logical_total
    if pt <= 0:
        return 0, logical_total
    logical_up = min(logical_total, max(0, round(logical_total * pu / pt)))
    return logical_up, logical_total


def count_aresone_pools_from_chassis(
    chassis_rows: List[Dict[str, Any]],
    line_rate_gbps: int,
) -> Dict[str, int]:
    """Aggregate RG-aware logical port pools across AresONE chassis rows."""
    mode = mode_for_line_rate(line_rate_gbps)
    if mode is None:
        return {
            "chassis_total": 0,
            "chassis_with_up_ports": 0,
            "cards_800ge": 0,
            "ports_up": 0,
            "ports_total": 0,
            "rg_capacity": 0,
            "max_ports_per_card": 0,
            "fanout_mode": "",
            "fanout_label": "",
        }

    chassis_total = 0
    chassis_up = 0
    ports_up = 0
    ports_total = 0
    cards_800ge = 0
    for ch in chassis_rows:
        if not _chassis_is_aresone(ch):
            continue
        chassis_total += 1
        ch_has_up = False
        for slot in ch.get("slots") or []:
            lu, lt = _logical_pool_from_slot(slot, mode)
            if lt <= 0:
                continue
            cards_800ge += 1
            ports_up += lu
            ports_total += lt
            if lu > 0:
                ch_has_up = True
        if ch_has_up:
            chassis_up += 1

    return {
        "chassis_total": chassis_total,
        "chassis_with_up_ports": chassis_up,
        "cards_800ge": cards_800ge,
        "ports_up": ports_up,
        "ports_total": ports_total,
        "rg_capacity": cards_800ge * ARESONE_RG_PER_CARD,
        "max_ports_per_card": mode.max_ports_per_card,
        "fanout_mode": mode.mode_id,
        "fanout_label": mode.label,
        "line_rate_gbps": line_rate_gbps,
    }


def build_aresone_pools_by_rate(
    chassis_rows: List[Dict[str, Any]],
) -> Dict[str, Dict[str, int]]:
    """Precompute logical port pools for each supported AresONE fanout line rate."""
    pools: Dict[str, Dict[str, int]] = {}
    for mode in FANOUT_MODES:
        key = str(mode.line_rate_gbps)
        pools[key] = count_aresone_pools_from_chassis(chassis_rows, mode.line_rate_gbps)
    return pools


def enrich_chassis_slots_with_pools(
    chassis_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach per-rate logical pool sizes to each 800GE slot for wizard display."""
    enriched: List[Dict[str, Any]] = []
    for ch in chassis_rows:
        row = dict(ch)
        slots_out: List[Dict[str, Any]] = []
        for slot in ch.get("slots") or []:
            slot_out = dict(slot)
            pools: Dict[str, Dict[str, int]] = {}
            for mode in FANOUT_MODES:
                lu, lt = _logical_pool_from_slot(slot, mode)
                if lt > 0:
                    pools[str(mode.line_rate_gbps)] = {
                        "ports_up": lu,
                        "ports_total": lt,
                        "rg_capacity": ARESONE_RG_PER_CARD,
                        "fanout_label": mode.label,
                    }
            if pools:
                slot_out["aresone_pools"] = pools
                slot_out["rg_capacity"] = ARESONE_RG_PER_CARD
            slots_out.append(slot_out)
        row["slots"] = slots_out
        enriched.append(row)
    return enriched


def _count_lab_aresone_ports(
    fabric_detail: Dict[str, Any],
    line_rate_gbps: int,
) -> Dict[str, int]:
    return count_aresone_pools_from_chassis(
        fabric_detail.get("chassis") or [],
        line_rate_gbps,
    )


def validate_lab_aresone_capacity(
    fabric_detail: Optional[Dict[str, Any]],
    port_count: int,
    line_rate: str,
    *,
    via_ocs: str = "either",
    require_link_up: bool = True,
) -> Dict[str, Any]:
    """Merge requirement plan with per-lab fabric snapshot."""
    plan = plan_aresone_requirement(port_count, line_rate)
    if not plan.get("ok"):
        out = dict(plan)
        out["submittable"] = False
        out["capacity_status"] = "error"
        return out

    out = dict(plan)
    fd = fabric_detail or {}
    gbps = plan["line_rate_gbps"]
    counts = _count_lab_aresone_ports(fd, gbps)
    out.update(counts)

    ocs_total = int((fd.get("ocs_paths") or {}).get("total") or 0)
    ocs_active = int((fd.get("ocs_paths") or {}).get("active") or 0)
    has_ocs = bool((fd.get("nodes_by_kind") or {}).get("ocs")) or ocs_total > 0
    out["lab_has_ocs"] = has_ocs
    out["ocs_paths_total"] = ocs_total
    out["ocs_paths_active"] = ocs_active

    ports_needed = plan["port_count"]
    rgs_needed = plan["rgs_needed"]
    pairs_needed = plan["pairs_needed"]

    ports_up = int(counts["ports_up"])
    ports_total = int(counts["ports_total"])
    if require_link_up:
        ports_avail = ports_up
    else:
        ports_avail = ports_total
    ports_ok = ports_avail >= ports_needed
    ports_maybe = (
        require_link_up
        and ports_total >= ports_needed
        and ports_up < ports_needed
    )
    rg_ok = counts["rg_capacity"] >= rgs_needed

    issues: List[str] = []
    if via_ocs == "required" and not has_ocs:
        issues.append("OCS required but topology has no OCS paths")
    elif via_ocs == "none" and has_ocs and ocs_total > 0:
        issues.append("Direct-only requested; lab uses OCS fanout cabling")

    if counts["cards_800ge"] == 0:
        issues.append("No 800GE AresONE cards seen in fabric snapshot")
    elif not rg_ok:
        issues.append(
            f"Need {rgs_needed} RG(s) ({plan['fanout_label']}) but only "
            f"{counts['rg_capacity']} RG capacity across {counts['cards_800ge']} card(s)"
        )
    pool_label = plan.get("fanout_label") or f"{gbps}G"
    if not ports_ok:
        issues.append(
            f"Lab pool shows {ports_up}↑ / {ports_total} "
            f"logical {gbps}G ports ({pool_label}); need {ports_needed}"
            + ("" if require_link_up else " (link-up not required)")
        )
    elif ports_maybe:
        issues.append(
            f"Only {ports_up}↑ of {ports_total} logical {gbps}G ports "
            f"({pool_label}) — uncheck “Require link up” or refresh fabric"
        )

    out["ports_avail"] = ports_avail
    out["require_link_up"] = require_link_up
    out["ports_sufficient"] = ports_ok
    out["rg_sufficient"] = rg_ok
    out["validation_ok"] = not issues
    out["submittable"] = True
    out["capacity_status"] = "ok" if not issues else "warn"
    out["validation_issues"] = issues
    if issues:
        out["validation_message"] = "; ".join(issues)
    else:
        out["validation_message"] = (
            f"Lab OK: {counts['ports_up']}↑ / {counts['ports_total']} logical {gbps}G "
            f"({plan['fanout_label']}), {counts['rg_capacity']} RG across "
            f"{counts['cards_800ge']} card(s)"
        )
    return out


def requirement_applies(chassis_family: str, line_rate: str) -> bool:
    """True when fanout planning is relevant (AresONE family or ≥50G line rate)."""
    fam = (chassis_family or "").lower()
    if fam in ("aresone", "ares"):
        return True
    gbps = parse_line_rate_gbps(line_rate)
    return gbps is not None and gbps >= 50
