"""
Enrich photonic site JSON with live LLDP and device status for validation / export.

Writes a new structure (copy of input) with per-entity ``connectivity`` blocks and
optional hostname sync. Does not persist to DB unless the caller does.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from connect.drivers import get_driver
from connect.eos_sonic_switch import ensure_eos_sonic_vendor_type
from connect.models import Device, KeysightChassis
from connect.topology_lldp import fetch_chassis_lldp


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lldp_rows(result) -> List[dict]:
    if not result or not result.success or not result.data:
        return []
    out: List[dict] = []
    for n in list(result.data)[:400]:
        if not isinstance(n, dict):
            continue
        out.append(
            {
                "local_port": n.get("local_port", "") or "",
                "remote_device": n.get("remote_device", "") or n.get("system_name", "") or "",
                "remote_port": n.get("remote_port", "") or "",
                "mgmt_ip": n.get("mgmt_ip", "") or "",
                "chassis_id": n.get("chassis_id", "") or "",
            }
        )
    return out


def _enrich_device_block(block: Optional[dict]) -> None:
    if not block or not isinstance(block, dict):
        return
    ip = (block.get("ip") or "").strip()
    if not ip:
        return
    d = Device.objects.filter(ip_address=ip).first()
    ckey = "connectivity"
    if not d:
        block[ckey] = {
            "validated_at": _iso_now(),
            "error": "no_device_in_labvault",
        }
        return
    lldp: List[dict] = []
    lldp_err: Optional[str] = None
    try:
        ensure_eos_sonic_vendor_type(d)
        drv = get_driver(d)
        lldp_res = drv.get_lldp_neighbors_detail()
        lldp = _lldp_rows(lldp_res)
        if lldp_res and not lldp_res.success:
            lldp_err = lldp_res.error
    except Exception as e:
        lldp_err = str(e)[:200]
    ocs_ip = (block.get("fixed_mapping") or {}).get("to_ocs", "").strip() if isinstance(block.get("fixed_mapping"), dict) else ""
    ocs_hits: List[dict] = []
    for row in lldp:
        mgmt = (row.get("mgmt_ip") or "").strip()
        rem = f"{row.get('remote_device', '')} {row.get('remote_port', '')}".lower()
        if ocs_ip and (ocs_ip in (mgmt or "") or ocs_ip in rem):
            ocs_hits.append(row)
    block[ckey] = {
        "validated_at": _iso_now(),
        "device_id": d.id,
        "vendor_type": d.vendor_type,
        "status": d.status,
        "hostname_db": d.hostname or "",
        "lldp_neighbor_count": len(lldp),
        "lldp_error": lldp_err,
        "lldp_edges": lldp,
        "ocs_ip_expected": ocs_ip or None,
        "lldp_rows_suggesting_ocs": ocs_hits if ocs_hits else None,
    }


def _enrich_ocs_block(block: Optional[dict]) -> None:
    _enrich_device_block(block)


def _enrich_ks_chassis_block(block: dict) -> None:
    ip = (block.get("ip") or "").strip()
    if not ip:
        return
    ckey = "connectivity"
    ch = KeysightChassis.objects.filter(ip_address=ip).first()
    if not ch:
        block[ckey] = {"validated_at": _iso_now(), "error": "no_chassis_in_labvault"}
        return
    comm = (ch.snmp_community or "public") or "public"
    try:
        snmp_lldp = fetch_chassis_lldp(ch.ip_address, community=comm, timeout=4)
    except Exception as e:
        snmp_lldp = []
        err = str(e)[:200]
    else:
        err = None
    neigh = [dict(n) for n in (snmp_lldp or []) if isinstance(n, dict)][:200]
    block[ckey] = {
        "validated_at": _iso_now(),
        "chassis_id": ch.id,
        "status": ch.status,
        "hostname_db": ch.hostname or "",
        "chassis_type_db": ch.chassis_type,
        "snmp_lldp_neighbor_count": len(neigh),
        "snmp_lldp_error": err,
        "snmp_lldp_edges": neigh,
    }


def enrich_ocs_site_json(
    data: dict,
    live_lldp: bool = True,
) -> dict:
    """Return a deep copy of `data` with `connectivity` added under each block.
    If ``live_lldp`` is False, only DB hostname/status (no on-device LLDP / SNMP walk).
    """
    out = copy.deepcopy(data)
    out["validation"] = {
        "enriched_at": _iso_now(),
        "live_lldp": bool(live_lldp),
        "description": "connectivity from LabVault DB" + ("" if live_lldp else " (db-only, no live LLDP)"),
    }
    if not live_lldp:
        _enrich_db_only_all(out)
        return out
    ocs = out.get("ocs_controller")
    if ocs and isinstance(ocs, dict):
        _enrich_ocs_block(ocs)
    for b in out.get("ares_switches") or []:
        if isinstance(b, dict):
            _enrich_device_block(b)
    for b in out.get("arista_switches") or []:
        if isinstance(b, dict):
            _enrich_device_block(b)
    for b in out.get("keysight_chassis") or []:
        if isinstance(b, dict):
            _enrich_ks_chassis_block(b)
    return out


def _enrich_db_only_all(out: dict) -> None:
    for label, blist, _model, is_ch in [
        ("ocs_controller", [out.get("ocs_controller")], False),
        ("ares_switches", out.get("ares_switches") or [], False),
        ("arista_switches", out.get("arista_switches") or [], False),
        ("keysight_chassis", out.get("keysight_chassis") or [], True),
    ]:
        items = [x for x in list(blist) if x]
        for b in items:
            if not isinstance(b, dict):
                continue
            ip = (b.get("ip") or "").strip()
            if not ip:
                continue
            if is_ch:
                ch = KeysightChassis.objects.filter(ip_address=ip).first()
                b["connectivity"] = {
                    "validated_at": _iso_now(),
                    "mode": "db_only",
                    "chassis_id": ch.id if ch else None,
                    "status": ch.status if ch else None,
                    "hostname_db": (ch.hostname or "") if ch else None,
                }
            else:
                d = Device.objects.filter(ip_address=ip).first()
                b["connectivity"] = {
                    "validated_at": _iso_now(),
                    "mode": "db_only",
                    "device_id": d.id if d else None,
                    "status": d.status if d else None,
                    "hostname_db": (d.hostname or "") if d else None,
                }


def apply_hostname_fixes(data: dict) -> Tuple[dict, List[str]]:
    """
    For each block that has connectivity.hostname_db, set `name` to match if empty or if forced.

    Returns (mutated copy, list of log lines).
    """
    log: List[str] = []
    out = copy.deepcopy(data)
    ocs = out.get("ocs_controller")
    blocks: List[Tuple[str, Any]] = []
    if ocs and isinstance(ocs, dict):
        blocks.append(("ocs_controller", ocs))
    for i, b in enumerate(out.get("ares_switches") or []):
        blocks.append((f"ares_switches[{i}]", b))
    for i, b in enumerate(out.get("arista_switches") or []):
        blocks.append((f"arista_switches[{i}]", b))
    for path, b in blocks:
        if not isinstance(b, dict):
            continue
        conn = b.get("connectivity") or {}
        hn = (conn.get("hostname_db") or "").strip()
        if not hn:
            continue
        old = (b.get("name") or "").strip()
        if old != hn:
            b["name"] = hn
            log.append(f"{path}: name {old!r} -> {hn!r}")
    for i, b in enumerate(out.get("keysight_chassis") or []):
        if not isinstance(b, dict):
            continue
        conn = b.get("connectivity") or {}
        hn = (conn.get("hostname_db") or "").strip()
        if not hn:
            continue
        old = (b.get("name") or "").strip()
        if old != hn and hn:
            b["name"] = hn
            log.append(f"keysight_chassis[{i}]: name {old!r} -> {hn!r}")
    return out, log
