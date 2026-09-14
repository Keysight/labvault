"""
OCS (optical circuit switch) driver — Calient / Photonic REST (e.g. S320).

**API documentation (Swagger UI):** ``https://<device>/api/`` — the served spec is
``/api/swagger.json``; **live calls** use ``basePath`` ``/rest`` (e.g.
``GET https://<device>/rest/info/?id=restversion``).

Authentication: HTTP Basic (device username/password). Optional JSON in
``Device.api_key``::

    {
      "verify_ssl": false,
      "rest_base": "/rest"
    }

``rest_base`` defaults to ``/rest`` and should match the controller’s OpenAPI
``basePath``.

**Dynamic switching** (cross-connects) is implemented via ``send_config`` using
JSON lines (one operation per list item), for example::

    {"op": "xconnect_add", "in": "1.1.1/1", "out": "2.1.1/1", "group": "SYSTEM", "conn": "lab_a", "dir": "bi", "band": "CBAND"}
    {"op": "xconnect_delete", "conn": "1.1.1-2.1.1", "group": "SYSTEM", "name": "1.1.1-2.1.1"}
    {"op": "xconnect_activate", "conn": "...", "group": "SYSTEM", "name": "..."}
    {"op": "xconnect_deactivate", "conn": "...", "group": "SYSTEM", "name": "..."}

Legacy LLDP-style paths in ``rest_paths`` are no longer required for normal
operation; port/cross-connect state comes from ``/rest/ports`` and
``/rest/crossconnects``. SNMP LLDP (via :class:`KeysightDriver`) is still used
when present and is **merged** with cross-connect-derived adjacency (SNMP wins
on duplicate ``local_port``).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin

import requests
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning

from connect.ip_addressing import bracket_host

from .base import DriverResult
from .keysight import KeysightDriver, OID_SYS_DESCR

logger = logging.getLogger(__name__)
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

_DEFAULT_REST_LLDP_PATHS = [
    "/api/v1/lldp/neighbors",
    "/api/v1/lldp",
    "/api/lldp/neighbors",
    "/api/lldp",
]


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    t = str(v).strip().lower()
    return t in ("1", "true", "yes", "y", "on")


def _norm_dir(s: Any) -> str:
    x = (str(s or "bi")).strip().lower()
    if x in ("bi", "bidir", "bidi"):
        return "bi"
    if x in ("uni", "unidir", "u"):
        return "uni"
    return x or "bi"


class OcsDriver(KeysightDriver):
    VENDOR_NAME = "ocs"

    def __init__(self, device):
        super().__init__(device)
        self._api_opts: Dict[str, Any] = {}
        raw = (getattr(device, "api_key", None) or "").strip()
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                self._api_opts = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                self._api_opts = {}
        self._api_key_literal = raw if (not raw.startswith("{") or not self._api_opts) else ""
        self._rest_base = (self._api_opts.get("rest_base") or "/rest").strip() or "/rest"
        if not self._rest_base.startswith("/"):
            self._rest_base = "/" + self._rest_base

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.verify = _as_bool(self._api_opts.get("verify_ssl", False))
        s.auth = HTTPBasicAuth(self.username, self.password)
        h = self._api_opts.get("headers") or {}
        if isinstance(h, dict):
            s.headers.update(h)
        token = self._api_opts.get("bearer_token") or self._api_opts.get("token")
        if token and not s.headers.get("Authorization"):
            s.headers["Authorization"] = f"Bearer {token}"
        elif self._api_key_literal and not s.headers.get("X-API-Key"):
            s.headers["X-API-Key"] = self._api_key_literal
        s.headers.setdefault("Accept", "application/json")
        s.headers.setdefault("Content-Type", "application/json")
        return s

    def _origin(self, proto: str) -> str:
        host = bracket_host(self.ip)
        port = self.api_port
        if port:
            return f"{proto}://{host}:{port}"
        return f"{proto}://{host}"

    def _rest_root(self, proto: str = "https") -> str:
        return (self._origin(proto).rstrip("/") + self._rest_base).rstrip("/")

    def _rest_url(self, proto: str, rel: str, params: Optional[Dict[str, Any]] = None) -> str:
        base = self._rest_root(proto) + "/"
        path = rel.lstrip("/")
        u = urljoin(base, path)
        if params:
            q = urlencode(params, doseq=True)
            sep = "&" if "?" in u else "?"
            u = f"{u}{sep}{q}"
        return u

    def _rest_request(
        self,
        method: str,
        rel: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        timeout: int = 30,
    ) -> Tuple[Optional[requests.Response], Optional[str]]:
        last_err: Optional[str] = None
        saved_ip = self.ip
        for target in self.iter_connect_targets():
            self.ip = target
            for proto in ("https", "http"):
                try:
                    url = self._rest_url(proto, rel, params)
                    sess = self._session()
                    r = sess.request(
                        method,
                        url,
                        json=json_body,
                        timeout=timeout,
                    )
                    return r, None
                except (OSError, requests.RequestException) as e:
                    last_err = str(e)
                    logger.debug(
                        "OCS REST %s %s via %s/%s: %s", method, rel, target, proto, e,
                    )
        self.ip = saved_ip
        return None, last_err

    def _rest_get_json(self, rel: str, params: Dict[str, Any], timeout: int = 15) -> Any:
        r, err = self._rest_request("GET", rel, params=params, timeout=timeout)
        if r is None:
            raise OSError(err or "request failed")
        if r.status_code == 204:
            return None
        r.raise_for_status()
        if not (r.text or "").strip():
            return None
        return r.json()

    @staticmethod
    def _rest_http_error(r: requests.Response) -> str:
        text = (r.text or "").strip()
        if text:
            return f"HTTP {r.status_code}: {text[:500]}"
        return f"HTTP {r.status_code}"

    def _rest_post_json(self, rel: str, params: Dict[str, Any], body: Any, timeout: int = 30) -> Any:
        r, err = self._rest_request("POST", rel, params=params, json_body=body, timeout=timeout)
        if r is None:
            raise OSError(err or "request failed")
        if r.status_code >= 400:
            raise OSError(self._rest_http_error(r))
        if r.status_code == 204:
            return None
        try:
            return r.json()
        except ValueError:
            return {"raw": (r.text or "")[:2000], "status_code": r.status_code}

    def _rest_delete_json(self, rel: str, params: Dict[str, Any], timeout: int = 30) -> Any:
        r, err = self._rest_request("DELETE", rel, params=params, timeout=timeout)
        if r is None:
            raise OSError(err or "request failed")
        if r.status_code >= 400:
            raise OSError(self._rest_http_error(r))
        if r.status_code == 204:
            return None
        try:
            return r.json()
        except ValueError:
            return {"raw": (r.text or "")[:2000], "status_code": r.status_code}

    def get_base_url(self, proto=None) -> str:
        p = proto or ("https" if str(self.transport).lower() != "http" else "http")
        return self._rest_root(p)

    def probe(self) -> str:
        r, _ = self._rest_request("GET", "info/", params={"id": "restversion"}, timeout=5)
        if r is not None:
            if r.status_code == 200:
                return "ok"
            if r.status_code in (401, 403):
                return "auth_failed"
        saved_ip = self.ip
        for target in self.iter_connect_targets():
            self.ip = target
            val = self._snmp_get(OID_SYS_DESCR)
            if val and "No Such" not in str(val):
                return "ok"
        self.ip = saved_ip
        return "unreachable"

    def get_system_info(self) -> DriverResult:
        host = self.ip
        version = ""
        model = "OCS"
        serial = ""
        try:
            j = self._rest_get_json("info/", {"id": "softwareversion"})
            if isinstance(j, dict) and j.get("version"):
                version = str(j["version"])[:200]
        except Exception as e:
            logger.debug("OCS softwareversion: %s", e)
        try:
            j = self._rest_get_json("node/", {"id": "summary", "detail": "SOFTWARE"})
            if isinstance(j, list) and j and isinstance(j[0], dict):
                version = str(j[0].get("version") or version)[:200]
        except Exception as e:
            logger.debug("OCS node SOFTWARE: %s", e)
        try:
            j = self._rest_get_json("node/", {"id": "summary", "detail": "SYSCFG"})
            if isinstance(j, dict):
                serial = str(j.get("serialnumber") or "").strip()[:100]
                pn = str(j.get("partnumber") or "").strip()
                if pn:
                    model = pn[:100]
        except Exception as e:
            logger.debug("OCS node SYSCFG: %s", e)
        return DriverResult(
            success=True,
            data={
                "hostname": host,
                "version": version,
                "model_name": model,
                "serial_number": serial,
                "uptime": None,
            },
        )

    def get_interfaces(self) -> DriverResult:
        try:
            rows = self._rest_get_json("ports/", {"id": "summary"})
        except Exception as e:
            logger.warning("OCS ports summary failed %s: %s", self.ip, e)
            return DriverResult(success=True, data={"physical_data": [], "logical_data": []})
        physical: List[Dict[str, Any]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                pid = str(row.get("port") or "").strip()
                if not pid:
                    continue
                conn = str(row.get("conn") or row.get("connid") or "").strip()
                st = "connected" if conn else "up"
                physical.append(
                    {
                        "name": pid,
                        "status": st,
                        "description": f"OCS triplet {pid} conn={conn}" if conn else f"OCS port {pid}",
                        "display_name": pid,
                        "alias": (row.get("inalias") or row.get("outalias") or "") or "",
                        "ocs_connid": row.get("connid", ""),
                        "ocs_conn": conn,
                        "ocs_power": row.get("power", ""),
                    }
                )
        return DriverResult(
            success=True,
            data={
                "physical_data": physical,
                "logical_data": [],
                "ocs_rest": True,
            },
        )

    def fetch_crossconnect_list(self) -> List[Dict[str, Any]]:
        try:
            rows = self._rest_get_json("crossconnects/", {"id": "list"}, timeout=20)
        except Exception as e:
            logger.debug("OCS crossconnect list: %s", e)
            return []
        return rows if isinstance(rows, list) else []

    def get_ocs_crossconnects(
        self, raw_rows: Optional[List[Dict[str, Any]]] = None
    ) -> DriverResult:
        """Parse cross-connects for device detail: endpoints + both halves (power, loss, alarm, state).

        Pass ``raw_rows`` from :meth:`fetch_crossconnect_list` to avoid a second HTTP GET.
        Each returned dict has ``h1`` / ``h2`` sub-dicts (normalised strings) used by
        ``ocs_helpers.enrich_ocs_xconns`` for the power/loss/alarm table columns.
        """
        if raw_rows is None:
            raw_rows = self.fetch_crossconnect_list()
        out: List[Dict[str, Any]] = []
        for row in raw_rows or []:
            if not isinstance(row, dict):
                continue
            h1 = row.get("half1") or {}
            h2 = row.get("half2") or {}
            if not isinstance(h1, dict):
                h1 = {}
            if not isinstance(h2, dict):
                h2 = {}
            c1 = str(h1.get("conn") or "")
            m1 = re.match(r"([^>]+)>([^>]+)", c1)
            if not m1:
                continue
            # Normalise both halves into plain string dicts so enrich_ocs_xconns
            # can access inp/outp/loss/alarm/as/oc/os without KeyError.
            def _norm_half(h: dict) -> dict:
                hc: Dict[str, Any] = {}
                for k, v in h.items():
                    hc[k] = "" if v is None else str(v)
                for k in ("inp", "outp", "loss", "alarm", "as", "os", "oc", "conn", "state"):
                    hc.setdefault(k, str(h.get(k) or ""))
                return hc

            h1c = _norm_half(h1)
            h2c = _norm_half(h2)
            name = str(row.get("name") or row.get("connid") or "").strip()
            out.append(
                {
                    "name": name,
                    "n": name,
                    "group": str(row.get("group") or ""),
                    "dir": str(row.get("dir") or ""),
                    "band": str(row.get("band") or ""),
                    "port_a": m1.group(1).strip(),
                    "port_b": m1.group(2).strip(),
                    "h1": h1c,
                    "h2": h2c,
                }
            )
        return DriverResult(success=True, data=out)

    def _neighbors_from_crossconnect_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("connid") or "").strip()
            h1 = row.get("half1") or {}
            if not isinstance(h1, dict):
                continue
            c1 = str(h1.get("conn") or "")
            m = re.match(r"([^>]+)>([^>]+)", c1)
            if m:
                a, b = m.group(1).strip(), m.group(2).strip()
                label = f"xconnect:{name or c1}"
                out.append(
                    {
                        "local_port": a,
                        "remote_device": b,
                        "remote_port": b,
                        "chassis_id": "",
                        "mgmt_ip": "",
                        "system_name": label,
                        "source": "ocs_crossconnect",
                    }
                )
                out.append(
                    {
                        "local_port": b,
                        "remote_device": a,
                        "remote_port": a,
                        "chassis_id": "",
                        "mgmt_ip": "",
                        "system_name": label,
                        "source": "ocs_crossconnect",
                    }
                )
        return out

    def _neighbors_from_crossconnects(self) -> List[Dict[str, Any]]:
        return self._neighbors_from_crossconnect_rows(self.fetch_crossconnect_list())

    def get_lldp_neighbors_detail(
        self, *, crossconnect_rows: Optional[List[Dict[str, Any]]] = None
    ) -> DriverResult:
        ocs_links = (
            self._neighbors_from_crossconnect_rows(crossconnect_rows)
            if crossconnect_rows is not None
            else self._neighbors_from_crossconnects()
        )
        snmp_res = super().get_lldp_neighbors_detail()
        snmp_data: List[Dict[str, Any]] = []
        if snmp_res.success and isinstance(snmp_res.data, list):
            snmp_data = snmp_res.data
        snmp_ports = {n.get("local_port") for n in snmp_data if n.get("local_port")}
        merged = list(snmp_data)
        for n in ocs_links:
            lp = n.get("local_port")
            if lp and lp in snmp_ports:
                continue
            merged.append(n)
        if merged:
            return DriverResult(success=True, data=merged)
        extra = list(self._api_opts.get("rest_paths", []) or [])
        for path in extra + _DEFAULT_REST_LLDP_PATHS:
            for proto in ("https", "http"):
                try:
                    base = self._origin(proto)
                    r = self._session().get(f"{base}{path}", timeout=12)
                    if r.status_code != 200 or not (r.text or "").strip():
                        continue
                    neighbors = _parse_lldp_json_payload(r.text)
                    if neighbors:
                        for x in neighbors:
                            x.setdefault("source", "rest_lldp")
                        return DriverResult(success=True, data=neighbors + ocs_links)
                except (OSError, requests.RequestException, ValueError) as e:
                    logger.debug("OCS LLDP REST %s: %s", path, e)
        if ocs_links:
            return DriverResult(success=True, data=ocs_links)
        return snmp_res if snmp_res.data is not None else DriverResult(success=False, error=snmp_res.error or "no LLDP")

    def send_config(self, commands: list, commit: bool = True) -> DriverResult:
        """Apply cross-connect operations from JSON strings (see module docstring)."""
        results: List[Dict[str, Any]] = []
        for raw in commands or []:
            line = (raw or "").strip()
            if not line:
                continue
            try:
                op = json.loads(line)
            except json.JSONDecodeError as e:
                return DriverResult(success=False, error=f"Invalid JSON line: {e}: {line[:120]}")
            if not isinstance(op, dict):
                return DriverResult(success=False, error="Each command must be a JSON object")
            kind = str(op.get("op") or "").strip().lower()
            try:
                if kind == "xconnect_add":
                    body = {
                        "in": op.get("in") or op.get("inp"),
                        "out": op.get("out") or op.get("outp"),
                        "group": op.get("group") or "SYSTEM",
                        "conn": op.get("conn") or op.get("name"),
                        "dir": _norm_dir(op.get("dir", "bi")),
                    }
                    if op.get("band"):
                        body["band"] = str(op["band"])
                    if op.get("nolight") is not None:
                        body["nolight"] = str(op["nolight"]).lower()
                    if op.get("deadreckon") is not None:
                        body["deadreckon"] = str(op["deadreckon"]).lower()
                    res = self._rest_post_json("crossconnects/", {"id": "add"}, body)
                    results.append({"op": kind, "response": res})
                elif kind == "xconnect_badd":
                    arr = op.get("connections") or op.get("items")
                    if not isinstance(arr, list):
                        return DriverResult(success=False, error="xconnect_badd requires connections: []")
                    res = self._rest_post_json("crossconnects/", {"id": "badd"}, arr)
                    results.append({"op": kind, "response": res})
                elif kind == "xconnect_delete":
                    params = {
                        "id": "delete",
                        "conn": op.get("conn") or "",
                        "group": op.get("group") or "SYSTEM",
                        "name": op.get("name") or op.get("conn") or "",
                    }
                    res = self._rest_delete_json("crossconnects/", params)
                    results.append({"op": kind, "response": res})
                elif kind in ("xconnect_activate", "xconnect_deactivate"):
                    act = "activate" if kind == "xconnect_activate" else "deactivate"
                    params = {
                        "id": act,
                        "conn": op.get("conn") or "",
                        "group": op.get("group") or "SYSTEM",
                        "name": op.get("name") or op.get("conn") or "",
                    }
                    res = self._rest_post_json("crossconnects/", params, None)
                    results.append({"op": kind, "response": res})
                elif kind == "xconnect_deleteall":
                    res = self._rest_post_json("crossconnects/", {"id": "deleteall"}, {}, timeout=60)
                    results.append({"op": kind, "response": res})
                elif kind == "port_config":
                    res = self._rest_post_json("ports/", {"id": "config"}, op.get("body") or op, timeout=30)
                    results.append({"op": kind, "response": res})
                else:
                    return DriverResult(
                        success=False,
                        error=f"Unknown op {kind!r}; use xconnect_add, xconnect_delete, xconnect_badd, …",
                    )
            except OSError as e:
                return DriverResult(success=False, error=str(e), data=results)
            except Exception as e:
                return DriverResult(success=False, error=str(e), data=results)
        if not results:
            return DriverResult(success=False, error="No operations in command list")
        return DriverResult(success=True, data={"results": results, "commit": commit})

    def restore_patch_snapshot(
        self,
        connections: List[Dict[str, Any]],
        *,
        clear_first: bool = True,
    ) -> DriverResult:
        """Restore cross-connects via REST, falling back to TL1 when REST user is read-only."""
        from connect.drivers.ocs_tl1 import (
            is_rest_permission_denied,
            restore_connections_via_tl1,
            tl1_credentials_from_device,
        )

        commands: List[str] = []
        if clear_first:
            commands.append(json.dumps({"op": "xconnect_deleteall"}))
        if connections:
            commands.append(json.dumps({"op": "xconnect_badd", "connections": connections}))
        if not commands:
            return DriverResult(success=False, error="No patch pairs to restore")

        res = self.send_config(commands)
        if res.success:
            data = dict(res.data or {})
            data["method"] = "rest"
            return DriverResult(success=True, data=data)

        if not is_rest_permission_denied(res.error):
            return res

        creds = tl1_credentials_from_device(self.device)
        if not creds:
            return DriverResult(
                success=False,
                error=(
                    f"{res.error} — REST user cannot write cross-connects. "
                    "Add ssh_user, ssh_password, tl1_user, and tl1_password to the device "
                    "API key JSON (or tag device ocs-lab for lab defaults)."
                ),
                data=res.data,
            )

        host = self.ip or str(getattr(self.device, "ip_address", "") or "")
        tl1_res = restore_connections_via_tl1(
            host, creds, connections, clear_first=clear_first,
        )
        return tl1_res


def _parse_lldp_json_payload(text: str) -> List[Dict[str, Any]]:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return []
    found: List[Dict[str, Any]] = []
    _walk_lldp_obj(obj, found)
    return [n for n in found if n.get("local_port") and n.get("remote_device")]


def _walk_lldp_obj(obj: Any, out: List[Dict[str, Any]]) -> None:
    if obj is None:
        return
    if isinstance(obj, list):
        for x in obj:
            if isinstance(x, (dict, list)):
                _walk_lldp_obj(x, out)
        return
    if not isinstance(obj, dict):
        return
    local = _pick(
        obj,
        "local_port", "localPort", "localIf", "localInterface", "port", "ifName", "ifDescr",
    )
    remote = _pick(
        obj,
        "remote_device", "remoteSystemName", "remSysName", "sysName", "systemName", "chassisName",
    )
    rport = _pick(obj, "remote_port", "remotePort", "portId", "port_id", "remPortId")
    chassis = _pick(obj, "chassis_id", "chassisId", "remChassisId")
    mgmt = _pick(obj, "mgmt_ip", "managementAddress", "mgmtIp", "management_ip")

    if local and (remote or mgmt or chassis or rport):
        rd = remote or (str(chassis) if chassis else "") or (str(rport) if rport is not None else "") or "unknown"
        if not isinstance(rd, str):
            rd = str(rd)
        rport_s = (rport or "") if rport is None or isinstance(rport, str) else str(rport)
        ch_s = (chassis or "") if chassis is None or isinstance(chassis, str) else str(chassis)
        mg_s = (mgmt or "") if mgmt is None or isinstance(mgmt, str) else str(mgmt)
        row = {
            "local_port": str(local).strip(),
            "remote_device": (rd or "unknown").strip(),
            "remote_port": (rport_s or "").strip(),
            "chassis_id": (ch_s or "").strip(),
            "mgmt_ip": (mg_s or "").strip(),
            "system_name": (rd or "unknown").strip(),
        }
        out.append(row)
    else:
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk_lldp_obj(v, out)


def _pick(d: dict, *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    for k in keys:
        low = k.lower()
        for dk, dv in d.items():
            if isinstance(dk, str) and dk.lower() == low and dv not in (None, ""):
                return dv
    return None
