"""
OCS (photonic) device view helpers: triplet keys, shelf/bank grid, xconn table, patch lines, site mapping.

Ports are addressed as ``shelf.module.port`` triplets (optional ``/n`` suffix). The device
page renders ``OCS_PANEL_COUNT`` shelves, each made of banks of ``OCS_BANK_SIZE`` slots.
Everything here is pure data shaping over driver output and cached LLDP; nothing talks to
the controller. See ``docs/development/subsystems/ocs.md``.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from django.core.cache import cache

_OCS_KEY_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:/(\d+))?$")
OCS_BANK_SIZE = 8
# Keysight photonic OCS front panels (shelves 1–6)
OCS_PANEL_COUNT = 6


def norm_ocs_triplet_key(raw: str) -> str:
    """Normalize triplet strings so API / grid / data-ocs-triplet match."""
    t = (raw or "").strip()
    t = t.replace(" ", "")
    t = t.replace("\\", "/")
    return t


def parse_triplet_parts(key: str) -> Optional[Tuple[int, int, int, Optional[int]]]:
    """Return ``(shelf, module, port, suffix_or_None)`` for a normalized triplet, else None."""
    m = _OCS_KEY_RE.match(key or "")
    if not m:
        return None
    s, m_, p, br = m.groups()
    return int(s), int(m_), int(p), (int(br) if br else None)


def ocs_triplet_sort_key(triplet: str) -> Tuple[int, int, int, int]:
    """Numeric sort key for triplets; unparseable keys sort last."""
    m = _OCS_KEY_RE.match(triplet or "")
    if m:
        s, m_, p, b = m.groups()
        return (int(s), int(m_), int(p), int(b) if b else 0)
    return (9999, 9999, 9999, 9999)


def _ocs_led_for_port(row: Dict[str, Any]) -> str:
    """Green = actively cross-connected. Everything else = dark (off).
    Amber is no longer used — unpatched fiber ports render the same as empty slots."""
    st = (row.get("status") or "").lower()
    if st in ("connected", "up") and row.get("ocs_conn"):
        return "green"
    return "off"


def annotate_ocs_port(intf: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a driver ``physical_data`` row and add ``ocs_triplet_key``, ``short_name``, ``status_color``."""
    out = dict(intf)
    name = (out.get("name") or out.get("display_name") or "").strip()
    out["ocs_triplet_key"] = norm_ocs_triplet_key(name)
    out["short_name"] = name.split("/")[-1] if name else ""
    # Always recompute: ensures stale cached values can't leak
    out["status_color"] = _ocs_led_for_port(out)
    return out


def load_ocs_site_triplets(ocs_ip: str) -> Set[str]:
    """Triplets listed under site JSON `port_to_ocs_triplets` for the matching `ocs_controller.ip`."""
    out: Set[str] = set()
    base = Path(__file__).resolve().parent.parent / "resources"
    if not base.is_dir():
        return out
    target = (ocs_ip or "").strip()
    for path in sorted(base.glob("ocs_photonic_site*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        ctrl = data.get("ocs_controller") or {}
        if (ctrl.get("ip") or "").strip() != target:
            continue
        from .hbg_fabric_connectivity import ocs_fixed_mapping_is_active

        for sw_list_key in ("ares_switches", "arista_switches"):
            for sw in data.get(sw_list_key) or []:
                mapping = sw.get("fixed_mapping") or {}
                if not ocs_fixed_mapping_is_active(mapping):
                    continue
                fm = mapping.get("port_to_ocs_triplets") or {}
                for v in fm.values():
                    if isinstance(v, (list, tuple)):
                        for t in v:
                            out.add(norm_ocs_triplet_key(str(t)))
                    elif isinstance(v, str):
                        out.add(norm_ocs_triplet_key(v))
        # keysight_chassis (AresONE) with embedded [ocs_site_mapping] in notes
        import re as _re
        for ch in data.get("keysight_chassis") or []:
            if not isinstance(ch, dict):
                continue
            mapping = ch.get("fixed_mapping") or {}
            if ocs_fixed_mapping_is_active(mapping):
                fm = mapping.get("port_to_ocs_triplets") or {}
                for v in fm.values():
                    if isinstance(v, (list, tuple)):
                        for t in v:
                            out.add(norm_ocs_triplet_key(str(t)))
                    elif isinstance(v, str):
                        out.add(norm_ocs_triplet_key(v))
            notes_str = (ch.get("notes") or "").strip()
            if notes_str and "[ocs_site_mapping]" in notes_str:
                m = _re.search(r'\[ocs_site_mapping\]\s*(\{.+\})', notes_str, re.DOTALL)
                if m:
                    try:
                        sm = json.loads(m.group(1))
                        for v in (sm.get("port_to_ocs_triplets") or {}).values():
                            if isinstance(v, (list, tuple)):
                                for t in v:
                                    out.add(norm_ocs_triplet_key(str(t)))
                            elif isinstance(v, str):
                                out.add(norm_ocs_triplet_key(v))
                    except (json.JSONDecodeError, ValueError):
                        pass
    return out


def _find_xc_for_triplet(tri: str, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    t = norm_ocs_triplet_key(tri)
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        a = norm_ocs_triplet_key((r.get("port_a") or ""))
        b = norm_ocs_triplet_key((r.get("port_b") or ""))
        if t == a or t == b:
            return r
    return None


def _lldp_for_triplet(tri: str, lldp: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    t = norm_ocs_triplet_key(tri)
    for n in lldp or []:
        if not isinstance(n, dict):
            continue
        if norm_ocs_triplet_key((n.get("local_port") or "")) == t:
            return n
    return None


def _lldp_path_peer_phrase(n: Optional[Dict[str, Any]]) -> str:
    if not n:
        return ""
    ip = (n.get("mgmt_ip") or "").strip()
    rp = (n.get("remote_port") or "").strip()
    if ip and rp:
        return f"→ {ip} ({rp})"
    if ip:
        return f"→ {ip}"
    rd = (n.get("remote_device") or "").strip()
    if rd and not rd.startswith("xconnect:"):
        return f"→ {rd}"
    return ""


def enrich_ocs_ports_tooltips(
    flat: List[Dict[str, Any]],
    ocs_xconns: List[Dict[str, Any]],
    lldp_neighbors: List[Dict[str, Any]],
    site_triplets: Set[str],
    triplet_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """Add ocs_bank_label, ocs_tooltip_html to each port dict in place."""
    tm = triplet_map or {}
    for p in flat:
        tri = p.get("ocs_triplet_key") or ""
        parts = parse_triplet_parts(tri)
        if parts:
            s, m, pn, _ = parts
            p["ocs_bank_label"] = f"{s}.{m}"
            p["port_digit"] = str(pn) if 1 <= pn <= OCS_BANK_SIZE else (p.get("short_name") or "")
        else:
            p["ocs_bank_label"] = ""
            p["port_digit"] = p.get("short_name") or "?"
        in_site = bool(tri and tri in site_triplets)
        p["ocs_in_site_map"] = in_site
        row = _find_xc_for_triplet(tri, ocs_xconns)

        t_norm = norm_ocs_triplet_key(tri)
        partner = ""
        xc_name = ""
        this_h: Dict[str, Any] = {}
        peer_h: Dict[str, Any] = {}
        dir_str = ""
        band_str = ""
        if row:
            a = norm_ocs_triplet_key(row.get("port_a") or "")
            b = norm_ocs_triplet_key(row.get("port_b") or "")
            partner = b if t_norm == a else a
            xc_name = (row.get("n") or row.get("name") or "").strip()
            dir_str  = str(row.get("dir") or "").strip()
            band_str = str(row.get("band") or "").strip()
            if t_norm == a:
                this_h, peer_h = (row.get("h1") or {}), (row.get("h2") or {})
            else:
                this_h, peer_h = (row.get("h2") or {}), (row.get("h1") or {})

        def _pwr(h: Dict) -> str:
            i = str(h.get("inp") or "").strip()
            o = str(h.get("outp") or "").strip()
            if i and o: return f"{i} → {o} dBm"
            return (i or o) + " dBm" if (i or o) else ""

        def _loss(h: Dict) -> str:
            v = str(h.get("loss") or "").strip()
            return f"{v} dB" if v else ""

        def _state_badge(h: Dict) -> str:
            a_st = str(h.get("as") or "").strip()
            os_st = str(h.get("os") or "").strip()
            oc_st = str(h.get("oc") or "").strip()
            pts = []
            if a_st:
                col = "#22c55e" if a_st == "IS" else "#ef4444"
                pts.append(f"<span style='color:{col};font-weight:700;'>{html.escape(a_st)}</span>")
            if os_st and os_st != a_st:
                pts.append(f"<span class='text-muted'>OS:{html.escape(os_st)}</span>")
            if oc_st:
                pts.append(f"<span class='text-muted'>OC:{html.escape(oc_st)}</span>")
            return " ".join(pts)

        alarm_str = str(this_h.get("alarm") or "").strip()
        site_info = tm.get(t_norm) if tm else None
        nbr = _lldp_for_triplet(tri, lldp_neighbors)
        lldp_chassis = str((nbr or {}).get("chassis_id") or "").strip()
        hr = "<hr style='border-color:#1e293b;margin:4px 0;'>"

        lines: List[str] = []

        # Header
        state_html = _state_badge(this_h)
        lines.append(
            f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
            f"<strong style='font-size:0.82rem;color:#e2e8f0;'>{html.escape(tri)}</strong>"
            f"<span style='font-size:0.62rem;color:#475569;'>bank {html.escape(p.get('ocs_bank_label') or '')}</span>"
            "</div>"
        )
        if state_html:
            lines.append(f"<div style='font-size:0.65rem;margin-top:1px;'>{state_html}</div>")

        # XC partner
        if partner:
            lines.append(hr)
            lines.append(f"<div style='font-size:0.72rem;'><span style='color:#64748b;'>XC\u00a0partner\u00a0</span>"
                         f"<span style='color:#fb923c;font-weight:700;'>⇔ {html.escape(partner)}</span></div>")
            meta_parts = [x for x in [dir_str, band_str] if x]
            if meta_parts:
                lines.append(f"<div style='font-size:0.62rem;color:#475569;'>" + html.escape('  '.join(meta_parts)) + "</div>")

        # Power & Loss
        pwr_this = _pwr(this_h); loss_this = _loss(this_h)
        pwr_peer = _pwr(peer_h); loss_peer = _loss(peer_h)
        if pwr_this or pwr_peer:
            lines.append(hr)
            def _pwr_row(label, pwr, loss):
                r = f"<div style='font-size:0.68rem;'><span style='color:#64748b;'>{label}\u00a0</span>"
                r += f"<span style='color:#cbd5e1;'>{html.escape(pwr)}</span>"
                if loss:
                    r += f"\u00a0\u00a0<span style='color:#64748b;'>loss\u00a0</span><span style='color:#fbbf24;'>{html.escape(loss)}</span>"
                r += "</div>"
                return r
            if pwr_this: lines.append(_pwr_row('Pwr ↓', pwr_this, loss_this))
            if pwr_peer: lines.append(_pwr_row('Pwr ↑', pwr_peer, loss_peer))

        # Alarm
        if alarm_str:
            lines.append(f"<div style='font-size:0.65rem;color:#f87171;margin-top:1px;'>⚠ Alarm: {html.escape(alarm_str)}</div>")

        # Site device (switch / chassis)
        if site_info:
            sw_hn   = html.escape(site_info.get('hostname') or site_info.get('ip') or '?')
            sw_ip   = html.escape(site_info.get('ip') or '')
            sw_port = html.escape(site_info.get('sw_port') or '')
            lines.append(hr)
            lines.append("<div style='font-size:0.63rem;color:#475569;margin-bottom:1px;'>↳ Connected device</div>")
            ip_part = f"\u00a0<span style='color:#475569;font-size:0.63rem;'>{sw_ip}</span>" if sw_ip and sw_ip != sw_hn else ''
            lines.append(f"<div><span style='color:#38bdf8;font-weight:600;font-size:0.72rem;'>{sw_hn}</span>{ip_part}</div>")
            if sw_port:
                lines.append(f"<div style='font-size:0.68rem;color:#7dd3fc;'>Port:&nbsp;{sw_port}</div>")

        # LLDP
        if nbr:
            rd   = str(nbr.get('remote_device') or '').strip()
            mgmt = str(nbr.get('mgmt_ip') or '').strip()
            rp   = str(nbr.get('remote_port') or '').strip()
            src  = str(nbr.get('source') or '').strip()
            if rd or mgmt:
                lines.append(hr)
                lines.append("<div style='font-size:0.63rem;color:#475569;margin-bottom:1px;'>LLDP neighbor</div>")
                if rd:
                    lines.append(f"<div style='font-size:0.7rem;color:#a78bfa;font-weight:600;'>{html.escape(rd)}</div>")
                if mgmt:
                    lines.append(f"<div style='font-size:0.65rem;color:#7c3aed;'>IP:&nbsp;{html.escape(mgmt)}</div>")
                if rp:
                    lines.append(f"<div style='font-size:0.65rem;color:#7c3aed;'>Port:&nbsp;{html.escape(rp)}</div>")
                if lldp_chassis:
                    lines.append(f"<div style='font-size:0.62rem;color:#334155;'>Chassis:&nbsp;{html.escape(lldp_chassis)}</div>")

        if not in_site:
            lines.append("<div style='font-size:0.6rem;color:#1e293b;margin-top:2px;'>Not in site map</div>")

        p["ocs_tooltip_html"] = "".join(lines)



def _group_ocs_shelves_eight(ports: List[Dict[str, Any]], site_triplets: Set[str]) -> List[Dict[str, Any]]:
    """One horizontal row of banks per shelf; each bank has exactly eight port slots (1..8)."""
    by_mod: Dict[Tuple[int, int], List[Optional[Dict[str, Any]]]] = {}
    overflow: List[Dict[str, Any]] = []
    for p in ports:
        key = p.get("ocs_triplet_key") or ""
        pr = parse_triplet_parts(key)
        if not pr:
            continue
        s, m, pn, _ = pr
        sm = (s, m)
        if pr[2] < 1 or pr[2] > OCS_BANK_SIZE:
            overflow.append(p)
            continue
        if sm not in by_mod:
            by_mod[sm] = [None] * OCS_BANK_SIZE
        slot = pr[2] - 1
        if by_mod[sm][slot] is not None:
            overflow.append(p)
        else:
            by_mod[sm][slot] = p
    for p in overflow:
        key = p.get("ocs_triplet_key") or ""
        pr = parse_triplet_parts(key)
        if not pr:
            continue
        sm = (pr[0], pr[1])
        if sm not in by_mod:
            by_mod[sm] = [None] * OCS_BANK_SIZE
        arr = by_mod[sm]
        placed = False
        for i in range(OCS_BANK_SIZE):
            if arr[i] is None:
                arr[i] = p
                placed = True
                break
        if not placed:
            pass
    by_shelf: Dict[int, List[Tuple[int, List[Optional[Dict[str, Any]]]]]] = {}
    for (s, m), arr in by_mod.items():
        if s not in by_shelf:
            by_shelf[s] = []
        filled: List[Dict[str, Any]] = []
        for i in range(OCS_BANK_SIZE):
            tri = f"{s}.{m}.{i + 1}"
            if arr[i] is not None:
                cell = dict(arr[i])
                cell["port_digit"] = str(i + 1)
                if "ocs_tooltip_html" not in cell:
                    cell["ocs_tooltip_html"] = f"<strong>{html.escape(tri)}</strong><br>—"
                filled.append(cell)
            else:
                in_site = tri in site_triplets
                map_line = (
                    ""
                    if in_site
                    else (
                        '<span class="d-block mt-1"><span class="badge bg-secondary" style="font-size:0.6rem;">—</span> '
                        '<span style="font-size:0.65rem;">This triplet is not in [ocs_site_mapping] (no switch port / '
                        "peer link mapped for path verify)</span></span>"
                    )
                )
                ph = (
                    f"<strong>{html.escape(tri)}</strong> <span class='text-muted'>(bank {html.escape(f'{s}.{m}')})</span><br>"
                    "<span>Connection: —</span><br><span>Power: —</span><br><span>Path peer: —</span><br>"
                    "<span>Alarm: —</span><br><span>Admin: —</span>" + map_line
                )
                filled.append(
                    {
                        "ocs_triplet_key": tri,
                        "name": tri,
                        "status_color": "off",
                        "is_ocs_placeholder": True,
                        "port_digit": str(i + 1),
                        "ocs_bank_label": f"{s}.{m}",
                        "ocs_in_site_map": in_site,
                        "ocs_tooltip_html": ph,
                    }
                )
        by_shelf[s].append((m, filled))
    shelf_list: List[Dict[str, Any]] = []
    for s in sorted(by_shelf.keys()):
        mod_list: List[Dict[str, Any]] = []
        for mid, cells in sorted(by_shelf[s], key=lambda x: x[0]):
            mod_list.append(
                {
                    "module_id": mid,
                    "label": f"{s}.{mid}",
                    "ports": cells,
                }
            )
        nmod = len(mod_list)
        shelf_list.append(
            {
                "shelf_id": s,
                "label": f"Shelf {s}",
                "bank_count": nmod,
                "modules": mod_list,
            }
        )
    return ensure_ocs_shelf_panels(shelf_list, site_triplets)


def _placeholder_bank_cells(
    shelf_id: int,
    module_id: int,
    site_triplets: Set[str],
) -> List[Dict[str, Any]]:
    """Eight empty OCS port cells for a bank row (device page + port fabric)."""
    cells: List[Dict[str, Any]] = []
    for i in range(OCS_BANK_SIZE):
        tri = f"{shelf_id}.{module_id}.{i + 1}"
        in_site = tri in site_triplets
        map_line = (
            ""
            if in_site
            else (
                '<span class="d-block mt-1"><span class="badge bg-secondary" style="font-size:0.6rem;">—</span> '
                '<span style="font-size:0.65rem;">Not in site OCS map</span></span>'
            )
        )
        ph = (
            f"<strong>{html.escape(tri)}</strong> "
            f"<span class='text-muted'>(bank {html.escape(f'{shelf_id}.{module_id}')})</span><br>"
            "<span>Connection: —</span><br><span>Power: —</span><br>"
            "<span>Path peer: —</span><br><span>Alarm: —</span><br><span>Admin: —</span>"
            + map_line
        )
        cells.append(
            {
                "ocs_triplet_key": tri,
                "name": tri,
                "status_color": "off",
                "is_ocs_placeholder": True,
                "port_digit": str(i + 1),
                "ocs_bank_label": f"{shelf_id}.{module_id}",
                "ocs_in_site_map": in_site,
                "ocs_tooltip_html": ph,
            }
        )
    return cells


def ensure_ocs_shelf_panels(
    shelf_list: List[Dict[str, Any]],
    site_triplets: Set[str],
    panel_count: int = OCS_PANEL_COUNT,
) -> List[Dict[str, Any]]:
    """Always show all OCS front panels (default 6), even when empty or unpatched."""
    by_id = {int(s.get("shelf_id") or 0): s for s in shelf_list}
    out = list(shelf_list)
    for shelf_id in range(1, panel_count + 1):
        if shelf_id in by_id:
            continue
        mod_ids: Set[int] = set()
        for t in site_triplets:
            pr = parse_triplet_parts(t)
            if pr and pr[0] == shelf_id:
                mod_ids.add(pr[1])
        if not mod_ids:
            mod_ids = {0}
        mod_list = [
            {
                "module_id": mid,
                "label": f"{shelf_id}.{mid}",
                "ports": _placeholder_bank_cells(shelf_id, mid, site_triplets),
            }
            for mid in sorted(mod_ids)
        ]
        out.append(
            {
                "shelf_id": shelf_id,
                "label": f"Shelf {shelf_id}",
                "bank_count": len(mod_list),
                "modules": mod_list,
            }
        )
    return sorted(out, key=lambda x: int(x.get("shelf_id") or 0))




def _add_triplets_from_mapping(
    fm: Dict[str, Any],
    sw_ip: str,
    sw_name: str,
    dev: Any,
    triplet_map: Dict[str, Dict[str, Any]],
) -> None:
    """Populate triplet_map from a port_to_ocs_triplets dict."""
    dev_id = dev.pk if dev else None
    dev_hostname = (getattr(dev, "hostname", None) or sw_name or sw_ip)
    for sw_port, triplets in fm.items():
        if isinstance(triplets, str):
            triplets = [triplets]
        for t in triplets or []:
            k = norm_ocs_triplet_key(str(t))
            if k and k not in triplet_map:
                triplet_map[k] = {
                    "hostname": dev_hostname,
                    "ip": sw_ip,
                    "device_id": dev_id,
                    "sw_port": sw_port,
                }


def build_ocs_triplet_map(ocs_ip: str, all_devices: Iterable) -> Dict[str, Dict[str, Any]]:
    """
    Build {triplet_key: {hostname, ip, device_id, sw_port}} from:
    1. ocs_photonic_site*.json  ares_switches / arista_switches / keysight_chassis
    2. Device.notes rows with [ocs_site_mapping] that reference this OCS IP

    Used for cross-connect peer resolution and path-verify badges.
    """
    import re as _re
    devices_list = list(all_devices)
    notes_stamp = 0
    for dev in devices_list:
        ts = getattr(dev, 'updated_at', None)
        if ts is not None:
            notes_stamp = max(notes_stamp, int(ts.timestamp()))
    cache_key = f'ocs:triplet_map:{(ocs_ip or "").strip()}:{notes_stamp}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    devices_by_ip: Dict[str, Any] = {}
    for d in devices_list:
        ip = (getattr(d, "ip_address", None) or "").strip()
        if ip:
            devices_by_ip[ip] = d

    triplet_map: Dict[str, Dict[str, Any]] = {}
    target = (ocs_ip or "").strip()

    # --- Source 1: site JSON files ---
    base = Path(__file__).resolve().parent.parent / "resources"
    if base.is_dir():
        for fpath in sorted(base.glob("ocs_photonic_site*.json")):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            ctrl = data.get("ocs_controller") or {}
            if (ctrl.get("ip") or "").strip() != target:
                continue
            # ares_switches + arista_switches (legacy + current)
            from .hbg_fabric_connectivity import ocs_fixed_mapping_is_active

            for sw_list_key in ("ares_switches", "arista_switches"):
                for sw in data.get(sw_list_key) or []:
                    sw_ip = (sw.get("ip") or "").strip()
                    if not sw_ip:
                        continue
                    sw_name = (sw.get("name") or sw_ip).strip()
                    mapping = sw.get("fixed_mapping") or {}
                    if not ocs_fixed_mapping_is_active(mapping):
                        continue
                    fm = mapping.get("port_to_ocs_triplets") or {}
                    if fm:
                        _add_triplets_from_mapping(fm, sw_ip, sw_name, devices_by_ip.get(sw_ip), triplet_map)
            # keysight_chassis entries (AresONE + any chassis with embedded fixed_mapping or notes)
            for ch in data.get("keysight_chassis") or []:
                if not isinstance(ch, dict):
                    continue
                ch_ip = (ch.get("ip") or "").strip()
                if not ch_ip:
                    continue
                ch_name = (ch.get("name") or ch_ip).strip()
                # Explicit fixed_mapping in JSON
                mapping = ch.get("fixed_mapping") or {}
                if ocs_fixed_mapping_is_active(mapping):
                    fm = mapping.get("port_to_ocs_triplets") or {}
                    if fm:
                        _add_triplets_from_mapping(fm, ch_ip, ch_name, devices_by_ip.get(ch_ip), triplet_map)
                # [ocs_site_mapping] embedded in notes field of JSON entry
                notes_str = (ch.get("notes") or "").strip()
                if notes_str and "[ocs_site_mapping]" in notes_str:
                    m2 = _re.search(r'\[ocs_site_mapping\]\s*(\{.+\})', notes_str, re.DOTALL)
                    if m2:
                        try:
                            sm = json.loads(m2.group(1))
                            if (sm.get("to_ocs") or "").strip() == target:
                                fm2 = sm.get("port_to_ocs_triplets") or {}
                                _add_triplets_from_mapping(fm2, ch_ip, ch_name, devices_by_ip.get(ch_ip), triplet_map)
                        except (json.JSONDecodeError, ValueError):
                            pass

    # --- Source 2: Device.notes with [ocs_site_mapping] in DB ---
    for dev in devices_list:
        notes = (getattr(dev, "notes", None) or "").strip()
        if not notes or "[ocs_site_mapping]" not in notes:
            continue
        m = _re.search(r'\[ocs_site_mapping\]\s*(\{.+\})', notes, re.DOTALL)
        if not m:
            continue
        try:
            sm = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if (sm.get("to_ocs") or "").strip() != target:
            continue
        fm = sm.get("port_to_ocs_triplets") or {}
        sw_ip = (getattr(dev, "ip_address", None) or "").strip()
        sw_name = (getattr(dev, "hostname", None) or sw_ip)
        if fm:
            _add_triplets_from_mapping(fm, sw_ip, sw_name, dev, triplet_map)

    cache.set(cache_key, triplet_map, 300)
    return triplet_map


def _path_badge(triplet: str, triplet_map: Dict[str, Dict], lldp: Iterable[Dict], devices_by_ip: Dict) -> Dict[str, str]:
    """
    Return {'badge_class': ..., 'badge_text': ..., 'badge_title': ...} for path A/B column.
    Logic: triplet not in site map → grey "—"; in map but no LLDP cache → amber "No switch cache";
           in map + LLDP match → green "OK".
    """
    t = norm_ocs_triplet_key(triplet)
    info = triplet_map.get(t)
    if not info:
        return {
            "badge_class": "bg-secondary",
            "badge_text": "—",
            "badge_title": "This triplet is not in [ocs_site_mapping] (no switch port / peer link mapped for path verify)",
        }
    dev_id = info.get("device_id")
    sw_port = info.get("sw_port") or ""
    # check if peer device has cached LLDP for this sw_port
    sw_ip = info.get("ip") or ""
    has_lldp = False
    for n in lldp or []:
        if not isinstance(n, dict):
            continue
        lp = norm_ocs_triplet_key((n.get("local_port") or ""))
        if lp == t:
            src = n.get("source") or ""
            rd = n.get("remote_device") or ""
            if src != "ocs_crossconnect" and not rd.startswith("xconnect:"):
                has_lldp = True
                break
    if has_lldp:
        return {
            "badge_class": "bg-success",
            "badge_text": "OK",
            "badge_title": f"LLDP match found on OCS port {t}",
        }
    hn = info.get("hostname") or sw_ip
    return {
        "badge_class": "bg-warning text-dark",
        "badge_text": "No switch cache",
        "badge_title": f"Refresh {hn} in LabVault to compare live LLDP to this OCS",
    }


def _lookup_lldp_name(port: str, lldp: Iterable[Dict[str, Any]], devices_by_ip) -> str:
    port = (port or "").strip()
    for n in lldp or []:
        if (n.get("local_port") or "").strip() != port:
            continue
        rd = (n.get("remote_device") or "").strip()
        ip = (n.get("mgmt_ip") or "").strip()
        if ip and devices_by_ip and ip in devices_by_ip:
            d = devices_by_ip[ip]
            return d.hostname or d.ip_address
        if rd and rd not in ("unknown",) and not rd.startswith("xconnect:"):
            return rd
    return ""


def _fmt_power(inp, outp) -> str:
    i = str(inp).strip() if inp is not None else ""
    o = str(outp).strip() if outp is not None else ""
    if i and o:
        return f"{i}→{o}"
    return i or o or "—"


def enrich_ocs_xconns(
    rows: List[Dict[str, Any]],
    lldp_neighbors: List[Dict[str, Any]],
    devices: Iterable,
    triplet_map: Optional[Dict[str, Dict]] = None,
) -> List[Dict[str, Any]]:
    """Attach peer labels, path badges, power/loss/state from h1/h2 for the XConn table."""
    devices_by_ip: Dict[str, Any] = {}
    for d in devices:
        ip = (getattr(d, "ip_address", None) or "").strip()
        if ip:
            devices_by_ip[ip] = d

    tm = triplet_map or {}
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        a = norm_ocs_triplet_key((r.get("port_a") or ""))
        b = norm_ocs_triplet_key((r.get("port_b") or ""))
        h1 = r.get("h1") or {}
        h2 = r.get("h2") or {}
        x = dict(r)

        # LLDP-based peer names (fallback)
        x["peer_a"] = _lookup_lldp_name(a, lldp_neighbors, devices_by_ip)
        x["peer_b"] = _lookup_lldp_name(b, lldp_neighbors, devices_by_ip)

        # Site-map peer info (for device links)
        info_a = tm.get(a) or {}
        info_b = tm.get(b) or {}
        x["peer_a_hostname"] = info_a.get("hostname") or x["peer_a"] or "—"
        x["peer_a_ip"] = info_a.get("ip") or ""
        x["peer_a_device_id"] = info_a.get("device_id")
        x["peer_a_sw_port"] = info_a.get("sw_port") or ""
        x["peer_b_hostname"] = info_b.get("hostname") or x["peer_b"] or "—"
        x["peer_b_ip"] = info_b.get("ip") or ""
        x["peer_b_device_id"] = info_b.get("device_id")
        x["peer_b_sw_port"] = info_b.get("sw_port") or ""

        # Path badges
        x["path_a"] = _path_badge(a, tm, lldp_neighbors, devices_by_ip)
        x["path_b"] = _path_badge(b, tm, lldp_neighbors, devices_by_ip)

        # Power / loss from driver h1/h2
        x["pwr_a_str"] = _fmt_power(h1.get("inp"), h1.get("outp"))
        x["pwr_b_str"] = _fmt_power(h2.get("inp"), h2.get("outp"))
        x["pwr_combined"] = f"{x['pwr_a_str']} / {x['pwr_b_str']}"
        x["loss_a"] = str(h1.get("loss") or "—")
        x["loss_b"] = str(h2.get("loss") or "—")
        x["alarm_a"] = str(h1.get("alarm") or "—")

        state = str(h1.get("as") or "").strip()
        x["state_text"] = state or "—"
        x["state_css"] = "bg-success" if state == "IS" else ("bg-danger" if state else "bg-secondary")

        # Status color for row highlight
        if state == "IS" and not (h1.get("alarm") or "").strip():
            x["row_class"] = "table-success"
        elif state == "OOS":
            x["row_class"] = "table-danger"
        elif h1.get("alarm"):
            x["row_class"] = "table-warning"
        else:
            x["row_class"] = ""

        # n = cross-connect name
        if not x.get("n"):
            x["n"] = (r.get("name") or "").strip() or (f"{a}-{b}" if a and b else "")

        out.append(x)
    return out


def ocs_patch_pairs_for_ui(ocs_xconns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pairs for API + overlay: normalized triplets a, b, label n, and state for color coding."""
    pairs: List[Dict[str, Any]] = []
    for x in ocs_xconns or []:
        if not isinstance(x, dict):
            continue
        a = norm_ocs_triplet_key((x.get("port_a") or ""))
        b = norm_ocs_triplet_key((x.get("port_b") or ""))
        n = (x.get("n") or x.get("name") or "").strip() or f"{a}-{b}"
        state = (x.get("state_text") or "").strip()
        if a and b:
            pairs.append({"a": a, "b": b, "n": n, "state": state})
    return pairs


def build_ocs_shelves(
    physical_data: List[Dict[str, Any]],
    ocs_xconns: List[Dict[str, Any]],
    lldp_neighbors: List[Dict[str, Any]],
    ocs_ip: str,
    triplet_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Return (shelves for template, flat annotated ports).
    Enriches tooltips and pads each bank to eight ports.
    """
    site = load_ocs_site_triplets(ocs_ip)
    flat = [annotate_ocs_port(dict(p)) for p in (physical_data or []) if p]
    enrich_ocs_ports_tooltips(flat, ocs_xconns, lldp_neighbors, site, triplet_map)
    shelves = _group_ocs_shelves_eight(flat, site)
    return shelves, flat


def compute_ocs_summary(
    physical_data: List[Dict[str, Any]],
    ocs_xconns: List[Dict[str, Any]],
    lldp_neighbors: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Summary stats for the OCS overview cards (aligns with port-plane + path verify UI).
    """
    phys = physical_data or []
    xc = ocs_xconns or []
    n_ports = len(phys)
    n_xc = len(xc)

    paths_up = 0
    for p in phys:
        conn = (p.get("ocs_conn") or p.get("ocs_connid") or "").strip()
        st = (p.get("status") or "").lower()
        if conn and st in ("connected", "up"):
            paths_up += 1

    endpoints: Set[str] = set()
    for r in xc:
        if not isinstance(r, dict):
            continue
        a = norm_ocs_triplet_key((r.get("port_a") or ""))
        b = norm_ocs_triplet_key((r.get("port_b") or ""))
        if a:
            endpoints.add(a)
        if b:
            endpoints.add(b)

    lldp_total = len(endpoints) if endpoints else (2 * n_xc if n_xc else 0)
    lldp_ok = 0
    for n in lldp_neighbors or []:
        if not isinstance(n, dict):
            continue
        lp = norm_ocs_triplet_key((n.get("local_port") or ""))
        if not lp or lp not in endpoints:
            continue
        src = (n.get("source") or "").strip()
        if src == "ocs_crossconnect":
            continue
        rd = (n.get("remote_device") or "").strip()
        if rd.startswith("xconnect:"):
            continue
        lldp_ok += 1

    chassis_lldp = 0
    for n in lldp_neighbors or []:
        if not isinstance(n, dict):
            continue
        if (n.get("chassis_id") or "").strip():
            chassis_lldp += 1

    return {
        "active_xconns": n_xc,
        "ocs_ports": n_ports,
        "paths_up": paths_up,
        "lldp_path_ok": lldp_ok,
        "lldp_path_total": lldp_total,
        "chassis_in_lldp": chassis_lldp,
    }
