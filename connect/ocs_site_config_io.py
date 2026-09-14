"""
Shared helpers for photonic OCS site JSON: Device rows, Keysight chassis, and tag merge.

See resources/ocs_photonic_site_*.json and management command import_ocs_site_config.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction

from connect.ip_addressing import derive_dhcpv6_ocs_lab, normalize_ip
from connect.models import Device, KeysightChassis

logger = logging.getLogger(__name__)


def _site_addressing(data: dict) -> dict:
    raw = data.get("addressing")
    return raw if isinstance(raw, dict) else {}


def _mgmt_ipv6_from_block(b: dict, data: Optional[dict] = None) -> str:
    raw = normalize_ip(b.get("mgmt_ipv6") or b.get("ipv6") or "")
    if raw:
        return raw
    addr = _site_addressing(data or {})
    if (addr.get("ipv6_mode") or addr.get("ipv6_assignment")) == "dhcpv6":
        ip = (b.get("ip") or "").strip()
        return derive_dhcpv6_ocs_lab(ip) or ""
    return ""


def _ipv6_source_from_block(b: dict, data: dict) -> str:
    addr = _site_addressing(data)
    row = (b.get("mgmt_ipv6_source") or b.get("ipv6_mode") or "").strip().lower()
    if row in ("dhcpv6", "slaac", "static"):
        return row
    site = (addr.get("ipv6_mode") or addr.get("ipv6_assignment") or "").strip().lower()
    if site in ("dhcpv6", "slaac", "static"):
        return site
    v6 = _mgmt_ipv6_from_block(b, data)
    if v6 and site == "dhcpv6":
        return "dhcpv6"
    return ""


def _preferred_ip_from_block(b: dict, data: dict) -> str:
    """Site ``addressing.preferred_ip_version: dual`` applies to all imported rows."""
    site_pref = (_site_addressing(data).get("preferred_ip_version") or "").strip()
    row_pref = (b.get("preferred_ip_version") or "").strip()
    pref = row_pref or site_pref or "auto"
    v6 = _mgmt_ipv6_from_block(b, data)
    if pref == "auto" and site_pref == "dual":
        pref = "dual"
    if pref == "auto" and v6:
        pref = "dual"
    if pref not in ("auto", "ipv4", "ipv6", "dual"):
        pref = "auto"
    return pref


def merge_tags_ocs(common, device_tags, existing: str) -> str:
    """Merge common tags, per-device tags, and existing DB field (dedupe, stable order)."""
    merged: List[str] = []
    for t in (
        list(common or [])
        + list(device_tags or [])
        + [x.strip() for x in (existing or "").split(",") if x.strip()]
    ):
        if t and t not in merged:
            merged.append(t)
    return ",".join(merged)


def _team_tags_str(row: dict, common: List[str]) -> str:
    raw = row.get("team_tags")
    if raw is None:
        raw = row.get("tags")
    if isinstance(raw, list):
        parts = [str(x).strip() for x in raw if str(x).strip()]
    else:
        parts = [t.strip() for t in str(raw or "").split(",") if t.strip()]
    return merge_tags_ocs(common, parts, "")


def _stack_credentials_from_block(b: dict) -> dict:
    out: Dict[str, Any] = {}
    for key in ("arista_username", "arista_password", "sonic_username", "sonic_password"):
        if key in b and b[key] is not None:
            out[key] = b[key]
    return out


def _apply_stack_credentials(dev, b: dict) -> bool:
    creds = _stack_credentials_from_block(b)
    for key, val in creds.items():
        if getattr(dev, key, object()) != val:
            setattr(dev, key, val)
    return bool(creds)


def _notes_with_mapping(b: dict, dev_notes: str) -> str:
    fm = b.get("fixed_mapping")
    if not fm:
        return dev_notes
    line = f"[ocs_site_mapping] {json.dumps(fm, separators=(',', ':'))}"
    if line in (dev_notes or ""):
        return dev_notes
    if dev_notes and dev_notes.strip():
        return dev_notes.rstrip() + "\n\n" + line
    return line


def import_ocs_site_devices(data: dict) -> List[str]:
    """Create/update Device rows from ocs_controller, ares_switches, arista_switches. Returns log lines."""
    log: List[str] = []
    common = data.get("common_tags") or []
    site = (data.get("site") or "").strip()
    ocs_block = data.get("ocs_controller")

    all_blocks = []
    if ocs_block:
        all_blocks.append(("ocs", ocs_block))
    for b in data.get("ares_switches") or []:
        all_blocks.append(("ares", b))
    for b in data.get("arista_switches") or []:
        all_blocks.append(("arista", b))

    for kind, b in all_blocks:
        if not b or not b.get("ip"):
            continue
        ip = (b["ip"] or "").strip()
        name = (b.get("name") or ip).strip()
        dtags = list(b.get("tags") or [])
        vendor = b.get("vendor_type") or ""
        if kind == "ocs" and not vendor:
            vendor = "ocs"
        if kind == "ocs" and not name:
            name = f"OCS-{ip}"
        if not vendor:
            vendor = "arista"

        # Extract vendor_type_secondary from api_key JSON if present
        secondary_vendor = ""
        raw_api_key = b.get("api_key", "") or ""
        if raw_api_key:
            try:
                ak = json.loads(raw_api_key) if isinstance(raw_api_key, str) else raw_api_key
                secondary_vendor = ak.get("vendor_type_secondary", "") or ""
            except Exception:
                pass

        mgmt_v6 = _mgmt_ipv6_from_block(b, data)
        pref_ip = _preferred_ip_from_block(b, data)
        v6_src = _ipv6_source_from_block(b, data)
        base_defaults: Dict[str, Any] = {
            "username": b.get("username") or "admin",
            "password": b.get("password") or "CHANGE_ME",
            "vendor_type": vendor,
            "vendor_type_secondary": secondary_vendor,
            "transport": b.get("transport") or "https",
            "snmp_community": b.get("snmp_community", "public") or "public",
            "api_key": raw_api_key,
            "hostname": name,
            "site": (b.get("site") or site) or "",
            "notes": _notes_with_mapping(b, ""),
            "mgmt_ipv6": mgmt_v6,
            "mgmt_ipv6_source": v6_src,
            "preferred_ip_version": pref_ip,
        }
        base_defaults.update(_stack_credentials_from_block(b))

        dev, created = Device.objects.get_or_create(ip_address=ip, defaults=base_defaults)
        if not created:
            dev.username = b.get("username") or dev.username
            dev.password = b.get("password") or dev.password
            dev.vendor_type = vendor
            if secondary_vendor:
                dev.vendor_type_secondary = secondary_vendor
            dev.transport = b.get("transport") or dev.transport
            if b.get("snmp_community") is not None:
                dev.snmp_community = b.get("snmp_community") or "public"
            if b.get("api_key") is not None:
                dev.api_key = raw_api_key
            dev.hostname = name or dev.hostname
            if b.get("site") or site:
                dev.site = (b.get("site") or site) or dev.site
            if mgmt_v6 or b.get("mgmt_ipv6") is not None or b.get("ipv6") is not None:
                dev.mgmt_ipv6 = mgmt_v6
            if v6_src or _site_addressing(data).get("ipv6_mode"):
                dev.mgmt_ipv6_source = v6_src or dev.mgmt_ipv6_source
            if row_pref := (b.get("preferred_ip_version") or "").strip():
                dev.preferred_ip_version = row_pref
            elif _site_addressing(data).get("preferred_ip_version"):
                dev.preferred_ip_version = pref_ip
            _apply_stack_credentials(dev, b)

        dev.tags = merge_tags_ocs(common, dtags, dev.tags)
        new_notes = _notes_with_mapping(b, dev.notes or "")
        if new_notes != (dev.notes or ""):
            dev.notes = new_notes
        dev.save()
        log.append(
            f"{'Created' if created else 'Updated'} {dev.vendor_type} {dev.ip_address} ({name or dev.hostname})"
        )
    return log


def import_keysight_chassis_from_json(
    rows: List[dict],
    common: Optional[List[str]] = None,
    *,
    site_data: Optional[dict] = None,
) -> Tuple[int, int, List[str]]:
    """
    Create or update KeysightChassis from `keysight_chassis` array in site JSON.

    Each row supports: ip (required), name, username, password, chassis_type,
    snmp_community, site, lab_name, geo_location, team_tags / tags, notes.
    """
    if not rows:
        return 0, 0, []
    common = list(common or [])
    created_c = 0
    updated_c = 0
    errors: List[str] = []

    for row in rows:
        if not isinstance(row, dict):
            errors.append("skip: non-object row in keysight_chassis")
            continue
        ip = (row.get("ip") or "").strip()
        if not ip:
            errors.append("skip: keysight_chassis row without ip")
            continue
        team = _team_tags_str(row, common)
        mgmt_v6 = _mgmt_ipv6_from_block(row, site_data or {})
        pref_ip = _preferred_ip_from_block(row, site_data or {})
        v6_src = _ipv6_source_from_block(row, site_data or {})
        defaults: Dict[str, Any] = {
            "username": (row.get("username") or "admin").strip() or "admin",
            "password": (row.get("password") or "admin") or "admin",
            "chassis_type": (row.get("chassis_type") or "xgs12").strip() or "xgs12",
            "snmp_community": (row.get("snmp_community") or "public") or "public",
            "site": (row.get("site") or "").strip(),
            "lab_name": (row.get("lab_name") or "").strip(),
            "geo_location": (row.get("geo_location") or "").strip(),
            "team_tags": team,
            "notes": (row.get("notes") or "").strip(),
            "mgmt_ipv6": mgmt_v6,
            "mgmt_ipv6_source": v6_src,
            "preferred_ip_version": pref_ip,
        }
        ch, created = KeysightChassis.objects.get_or_create(ip_address=ip, defaults=defaults)
        if created:
            created_c += 1
        else:
            if row.get("username"):
                ch.username = defaults["username"]
            if row.get("password"):
                ch.password = str(row.get("password"))
            if row.get("chassis_type"):
                ch.chassis_type = str(row.get("chassis_type")).strip() or ch.chassis_type
            if row.get("snmp_community") is not None:
                ch.snmp_community = str(row.get("snmp_community") or "public")
            if mgmt_v6 or row.get("mgmt_ipv6") is not None or row.get("ipv6") is not None:
                ch.mgmt_ipv6 = mgmt_v6
            if v6_src or _site_addressing(site_data or {}).get("ipv6_mode"):
                ch.mgmt_ipv6_source = v6_src or ch.mgmt_ipv6_source
            if (row.get("preferred_ip_version") or "").strip() or _site_addressing(site_data or {}):
                ch.preferred_ip_version = pref_ip
            ch.site = (row.get("site") or "").strip() or ch.site
            ch.lab_name = (row.get("lab_name") or "").strip() or ch.lab_name
            ch.geo_location = (row.get("geo_location") or "").strip() or ch.geo_location
            ch.team_tags = team or ch.team_tags
            n = (row.get("notes") or "").strip()
            if n:
                ch.notes = n
            ch.save()
            updated_c += 1

        nm = (row.get("name") or "").strip()
        if nm and (created or row.get("sync_name_to_hostname")):
            ch.hostname = nm
            ch.save(update_fields=["hostname", "updated_at"])

    return created_c, updated_c, errors


def run_ocs_site_import(data: dict) -> Dict[str, Any]:
    """
    Import all Devices + keysight_chassis from a site dict (same shape as the JSON file).
    Caller should wrap in transaction.atomic() for all-or-nothing.
    """
    ocs = data.get("ocs_controller")
    ares = data.get("ares_switches") or []
    arista = data.get("arista_switches") or []
    ks_rows = data.get("keysight_chassis") or []

    if not ocs and not ares and not arista and not ks_rows:
        return {
            "ok": False,
            "error": "no ocs/ares/arista/keysight_chassis in JSON",
            "device_log": [],
        }

    dev_log: List[str] = []
    if ocs or ares or arista:
        dev_log = import_ocs_site_devices(data)

    ks_c, ks_u, ks_err = import_keysight_chassis_from_json(
        list(ks_rows),
        data.get("common_tags") or [],
        site_data=data,
    )
    return {
        "ok": True,
        "device_log": dev_log,
        "keysight_created": ks_c,
        "keysight_updated": ks_u,
        "keysight_errors": ks_err,
    }
