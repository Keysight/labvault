"""
Arista EOS driver — eAPI (JSON-RPC) when available, else SSH + FastCli with ``| json``.

Lab hosts often expose SSH:22 (show commands) but not HTTPS:443 eAPI; this driver tries eAPI
first, then falls back to SSH with the same username/password. Optional device ``api_key`` JSON:

- ``{"ssh_port": 22, "prefer_ssh": true}`` — ``prefer_ssh`` forces SSH first.
- ``{"auto_enable_eapi": true}`` — after a successful refresh over SSH, push ``management api http-commands``
  (opt-in; exposes HTTP/HTTPS on the management path—use with ACL/VRF as appropriate). Re-checks
  often; repeat ``send_config`` is throttled to once per hour (see ``eapi_config_force_retry``).
- ``{"eapi_vrf": "management"}`` — with ``auto_enable_eapi``, apply the ``vrf`` subcommand in that
  submode (omit for default-VRF lab devices).
- ``{"eapi_config_force_retry": true}`` — bypass the one-hour ``send_config`` throttle for this
  attempt (e.g. after fixing VRF or ``management`` defaults).
- ``{"eapi_cmd_schema_version": 2}`` — use eAPI JSON schema version **2** for ``runCmds`` (default **1**).
  Try **2** on recent platforms (e.g. DCS-7060X6) if some ``show * | json`` output fails or is empty
  (same HTTP ``command-api``; only the model revision changes).
- ``{"vendor_type_secondary": "sonic"}`` / ``{"active_os_detected": "eos"|"sonic"}`` — dual-OS
  hints (also available as ``Device`` fields ``vendor_type_secondary`` / ``dual_os_mode``).

Transport summary: eAPI ``POST {proto}://<host>:<port>/command-api`` (probe timeout 8 s; https then
http unless ``transport`` pins one; TLS verification disabled). SSH via Paramiko (connect timeout 16 s,
command timeout up to 200 s) with a per-device pooled session reused for ``SSH_IDLE_SEC``. Dual-OS boxes
delegate to :class:`~connect.drivers.sonic.SonicDriver` using the SONiC credential pair when the active
OS is SONiC.
"""
import json
import re
import shlex
import ssl
import logging
import threading
import time
import requests
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning
from jsonrpclib import Server
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseDriver, DriverResult

logger = logging.getLogger(__name__)
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
ssl._create_default_https_context = ssl._create_unverified_context

_sessions = {}
_protocol_cache = {}

# Per-device shared SSH (EOS FastCli) — one TCP session is reused for probe + all show commands in a
# refresh, and for ~SSH_IDLE_SEC after last use (poller / health) to avoid new handshakes every 20s.
_SSH_GLOCK = threading.RLock()
_SSH_POOL: Dict[int, dict] = {}
SSH_IDLE_SEC = 90


def _arista_pool_sweep() -> None:
    now = time.time()
    with _SSH_GLOCK:
        stale = [
            k
            for k, v in _SSH_POOL.items()
            if isinstance(k, int) and (now - v.get("last", 0)) > SSH_IDLE_SEC
        ]
        for k in stale:
            ent = _SSH_POOL.pop(k, None)
            if ent and ent.get("client"):
                try:
                    ent["client"].close()
                except Exception:
                    pass


def _arista_pool_transport_ok(client) -> bool:
    try:
        t = client.get_transport()
        return t is not None and t.is_active()
    except Exception:
        return False


def _arista_ssh_pool_close_device(device_id: int) -> None:
    with _SSH_GLOCK:
        ent = _SSH_POOL.pop(device_id, None)
        if ent and ent.get("client"):
            try:
                ent["client"].close()
            except Exception:
                pass


def _arista_pool_touch_device(device_id: int) -> None:
    """Mark pooled SSH as recently used (extends idle deadline). Safe no-op if no pool entry."""
    with _SSH_GLOCK:
        e = _SSH_POOL.get(device_id)
        if e:
            e["last"] = time.time()


def _arista_ssh_pool_close_ip(ip: str) -> None:
    ip = (ip or "").strip()
    if not ip:
        return
    to_del = [k for k, v in _SSH_POOL.items() if v.get("ip") == ip]
    for k in to_del:
        _arista_ssh_pool_close_device(k)


def _arista_ssh_raw_connect(
    host: str, port: int, username: str, password: str, banner_timeout: int = 22
):
    import paramiko
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        port=port,
        username=username,
        password=password,
        timeout=16,
        look_for_keys=False,
        allow_agent=False,
        banner_timeout=banner_timeout,
    )
    return c


def _get_session(ip):
    if ip not in _sessions:
        _sessions[ip] = requests.Session()
    return _sessions[ip]


def clear_cache(ip):
    """Drop cached HTTP session, detected eAPI protocol and pooled SSH for *ip*."""
    _sessions.pop(ip, None)
    _protocol_cache.pop(ip, None)
    _arista_ssh_pool_close_ip(ip)


def _arista_parse_json_block(text: str):
    if not (text or "").strip():
        return None
    t = text.strip()
    i = t.find("{")
    if i < 0:
        return None
    j = t.rfind("}")
    if j < i:
        return None
    try:
        return json.loads(t[i : j + 1])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def parse_show_version_text(text: str) -> dict:
    """Parse plain ``show version`` (or license) text when JSON is sparse (lab / new platforms)."""
    out: dict = {}
    if not (text or "").strip():
        return out
    joined = text.strip()
    m = re.search(r"^Hostname:\s*(.+)$", joined, re.MULTILINE | re.IGNORECASE)
    if m and m.group(1).strip():
        out["hostname"] = m.group(1).strip()[:200]
    m = re.search(r"^Serial number:\s*(\S+)", joined, re.MULTILINE | re.IGNORECASE)
    if m and m.group(1).strip() and m.group(1).strip().lower() not in (
        "none",
        "n/a",
        "unknown",
    ):
        out["serialNumber"] = m.group(1).strip()[:120]
    m = re.search(
        r"^System MAC address:\s*(\S+)", joined, re.MULTILINE | re.IGNORECASE
    )
    if m and m.group(1).strip():
        out["systemMacAddress"] = m.group(1).strip()[:32]
    m = re.search(r"^Platform:\s*(.+)$", joined, re.MULTILINE | re.IGNORECASE)
    if m and m.group(1).strip():
        out["modelName"] = m.group(1).strip()[:120]
    if "modelName" not in out:
        m = re.search(r"^Hardware model:\s*(.+)$", joined, re.MULTILINE | re.IGNORECASE)
        if m and m.group(1).strip():
            out["modelName"] = m.group(1).strip()[:120]
    m = re.search(
        r"(?:Arista\s+)?EOS[\s-]*version\s+([^\s,]+)|Software\s+version:\s*(\S+)|^Version\s*:\s*(\S+)",
        joined,
        re.MULTILINE | re.IGNORECASE,
    )
    if m:
        g = m.groups()
        v = next((x for x in g if x), None)
        if v:
            out["version"] = v[:120]
    return out


def _cpu_idle_from_top_json(cpu_info: Any) -> float:
    """Best-effort idle % from ``show processes top once`` JSON (varies by EOS / platform)."""
    if not isinstance(cpu_info, dict):
        return 100.0
    pct = cpu_info.get("%Cpu(s)", {})
    if isinstance(pct, dict) and "idle" in pct:
        try:
            return float(pct["idle"])
        except (TypeError, ValueError):
            pass
    if "idle" in cpu_info:
        try:
            return float(cpu_info["idle"])
        except (TypeError, ValueError):
            pass
    for _k, v in cpu_info.items():
        if isinstance(v, dict) and "idle" in v:
            try:
                return float(v["idle"])
            except (TypeError, ValueError):
                pass
    return 100.0


def _normalize_lldp_neighbors_summary(raw: Any) -> List[dict]:
    """``show lldp neighbors`` may return a list or per-interface map depending on EOS train."""
    if not isinstance(raw, dict):
        return []
    ln = raw.get("lldpNeighbors")
    if isinstance(ln, list):
        return [x for x in ln if isinstance(x, dict)]
    if isinstance(ln, dict):
        out: List[dict] = []
        for loc_port, v in ln.items():
            if isinstance(v, list):
                for n in v:
                    if isinstance(n, dict):
                        n2 = dict(n)
                        n2.setdefault("port", loc_port)
                        out.append(n2)
            elif isinstance(v, dict):
                for n in v.get("lldpNeighborInfo", []):
                    if isinstance(n, dict):
                        n2 = dict(n)
                        n2.setdefault("port", loc_port)
                        out.append(n2)
        return out
    return []


_ETHERNET_IFACE_RE = re.compile(r'^Ethernet\d', re.I)


def is_physical_ethernet_iface(name: str) -> bool:
    """True for physical Ethernet interfaces (not Vlan, Management, Port-Channel)."""
    n = (name or '').strip()
    if not _ETHERNET_IFACE_RE.match(n):
        return False
    if 'Vlan' in n or 'Management' in n or 'Port-Channel' in n:
        return False
    return True


def extract_input_discards_from_interface_info(info: dict) -> int:
    """Parse cumulative input discards from Arista ``show interfaces`` JSON row."""
    if not isinstance(info, dict):
        return 0
    ic = info.get('interfaceCounters') or info.get('counters') or {}
    if not isinstance(ic, dict):
        return 0
    for key in ('inDiscards', 'inputDiscards', 'inDiscardsTotal'):
        if key in ic and ic[key] is not None:
            try:
                return int(ic[key])
            except (TypeError, ValueError):
                return 0
    return 0


class AristaDriver(BaseDriver):
    """Arista EOS driver (eAPI first, SSH/FastCli fallback, optional EOS↔SONiC dual-OS).

    ``probe()`` sets ``_arista_mode`` to ``'eapi'`` or ``'ssh'``; every collector goes
    through ``_arista_run`` so both transports return the same eAPI-shaped JSON.
    Only the first connect target that probes ``ok`` is used for the rest of the call.
    """

    VENDOR_NAME = 'arista'

    def __init__(self, device):
        super().__init__(device)
        # Set by probe: 'eapi' (HTTPS/HTTP command-api) or 'ssh' (FastCli)
        self._arista_mode: Optional[str] = None
        # Set by _detect_active_os(): 'eos' or 'sonic' — used for dual-OS boxes
        self._active_os: Optional[str] = None

    def _eos_dict_opts(self) -> dict:
        raw = (getattr(self.device, "api_key", None) or "").strip()
        if raw.startswith("{"):
            try:
                d = json.loads(raw)
                return d if isinstance(d, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    def _ssh_port(self) -> int:
        p = self._eos_dict_opts().get("ssh_port")
        if p is not None:
            try:
                return int(p)
            except (TypeError, ValueError):
                pass
        return 22

    def _eapi_schema_version(self) -> int:
        """eAPI ``runCmds`` output schema revision (1=classic, 2=newer platforms). From ``api_key`` JSON."""
        v = self._eos_dict_opts().get("eapi_cmd_schema_version", 1)
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = 1
        return 2 if v == 2 else 1

    def _detect_protocol(self):
        if self.transport in ('http', 'https'):
            return self.transport
        return _protocol_cache.get(self.ip)

    def _probe_proto(self, proto):
        session = _get_session(self.ip)
        port = self.api_port or (443 if proto == 'https' else 80)
        url = f"{proto}://{self.ip}:{port}/command-api"
        payload = {
            "jsonrpc": "2.0", "method": "runCmds",
            "params": {"version": 1, "cmds": ["show version"], "format": "json"},
            "id": 1,
        }
        try:
            resp = session.post(url, auth=HTTPBasicAuth(self.username, self.password),
                                json=payload, verify=False, timeout=8)
            if resp.status_code == 200:
                _protocol_cache[self.ip] = proto
                return 'ok'
            elif resp.status_code == 401:
                _protocol_cache[self.ip] = proto
                return 'auth_failed'
            return 'unreachable'
        except requests.exceptions.SSLError:
            return 'unreachable'
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            return 'unreachable'
        except Exception:
            return 'unreachable'

    def _active_credentials(self) -> Tuple[str, str]:
        """Return (username, password) for the currently detected OS.
        EOS → Device.arista_username / arista_password
        SONiC → Device.sonic_username / sonic_password
        Falls back to device.username/password if no OS-specific creds available.
        When OS is unknown on a dual-OS box, prefer sonic credentials (on rotating boxes SONiC is
        usually the longer-running half of the cycle).
        """
        if self._active_os == 'sonic':
            u, p = self._sonic_credentials()
        elif self._active_os == 'unknown' and self._is_dual_os():
            # Unknown state on dual-OS box: try SONiC creds first
            u, p = self._sonic_credentials()
        else:
            u, p = self._eos_credentials()
        return u, p

    def _ensure_ssh_pooled(self) -> Tuple[Any, threading.RLock]:
        """Get or create a live pooled Paramiko client + run lock (serialized FastCli on one session).
        Uses OS-appropriate credentials when dual-OS mode is detected.
        """
        from paramiko.ssh_exception import AuthenticationException

        did = self.device.id
        _arista_pool_sweep()
        with _SSH_GLOCK:
            ent = _SSH_POOL.get(did)
            if ent and _arista_pool_transport_ok(ent.get("client")):
                ent["last"] = time.time()
                return ent["client"], ent["rlock"]
            if ent and ent.get("client"):
                try:
                    ent["client"].close()
                except Exception:
                    pass
                _SSH_POOL.pop(did, None)

        # Use OS-specific credentials when available
        ssh_user, ssh_pass = self._active_credentials()
        try:
            c = _arista_ssh_raw_connect(
                self.ip, self._ssh_port(), ssh_user, ssh_pass
            )
        except AuthenticationException:
            # If primary credentials fail and we have a secondary OS, try the other set
            if self._is_dual_os():
                other_user, other_pass = (
                    self._eos_credentials() if self._active_os == 'sonic'
                    else self._sonic_credentials()
                )
                if (other_user, other_pass) != (ssh_user, ssh_pass):
                    try:
                        c = _arista_ssh_raw_connect(
                            self.ip, self._ssh_port(), other_user, other_pass
                        )
                        # Update active OS based on which creds worked
                        self._active_os = 'eos' if self._active_os == 'sonic' else 'sonic'
                        logger.info("Arista %s: switched to %s credentials", self.ip, self._active_os)
                    except Exception:
                        raise AuthenticationException(f"Both EOS and SONiC credentials failed for {self.ip}")
                else:
                    raise
            else:
                raise
        except Exception as e:
            logger.debug("Arista SSH connect %s: %s", self.ip, e)
            raise
        rlock = threading.RLock()
        with _SSH_GLOCK:
            _arista_pool_sweep()
            ent = _SSH_POOL.pop(did, None)
            if ent and ent.get("client") and ent["client"] is not c:
                try:
                    ent["client"].close()
                except Exception:
                    pass
            _SSH_POOL[did] = {
                "client": c,
                "rlock": rlock,
                "last": time.time(),
                "ip": self.ip,
            }
        return c, rlock

    def _probe_ssh(self) -> str:
        try:
            from paramiko.ssh_exception import AuthenticationException
        except ImportError:
            return 'unreachable'

        try:
            c, rlock = self._ensure_ssh_pooled()
        except AuthenticationException:
            return "auth_failed"
        except Exception as e:
            logger.debug("Arista SSH probe connect %s: %s", self.ip, e)
            return "unreachable"

        def _read_exec(cmd: str, timeout: int = 30) -> str:
            with rlock:
                _, stdout, stderr = c.exec_command(cmd, timeout=timeout)
                return ((stdout.read() or b"") + (stderr.read() or b"")).decode(
                    "utf-8", errors="replace"
                )

        for inner in ("show version | json", "show version"):
            for prefix in ("FastCli", "Cli"):
                raw = _read_exec(f"{prefix} -c {shlex.quote(inner)}", 30)
                if not raw or len(raw) < 5:
                    continue
                if "json" in inner:
                    if _arista_parse_json_block(raw):
                        with _SSH_GLOCK:
                            e2 = _SSH_POOL.get(self.device.id)
                            if e2:
                                e2["last"] = time.time()
                        return "ok"
                else:
                    low = raw.lower()
                    if any(
                        s in low
                        for s in (
                            "arista",
                            "arista eos",
                            " eos",
                            "veos",
                            "ceos",
                            "dcs-",
                            "7060",
                            "7050",
                            "7280",
                        )
                    ):
                        with _SSH_GLOCK:
                            e2 = _SSH_POOL.get(self.device.id)
                            if e2:
                                e2["last"] = time.time()
                        return "ok"
        raw2 = _read_exec("show version", 25)
        rlow = raw2.lower()
        if len(raw2) > 30 and any(
            x in rlow for x in ("arista", "eos", "dcs", "version", "cvd")
        ):
            with _SSH_GLOCK:
                e2 = _SSH_POOL.get(self.device.id)
                if e2:
                    e2["last"] = time.time()
                    # SSH session drops directly into EOS shell — no FastCli/Cli wrapper needed
                    e2["direct_eos"] = True
            return "ok"
        _arista_ssh_pool_close_device(self.device.id)
        return "unreachable"

    def _ssh_is_direct_eos(self) -> bool:
        """Return True when the SSH session drops directly into EOS (no FastCli/Cli wrapper)."""
        with _SSH_GLOCK:
            ent = _SSH_POOL.get(self.device.id)
            return bool(ent and ent.get("direct_eos"))

    def _ssh_run_direct(
        self, client: Any, rlock: threading.RLock, inner: str, as_json: bool
    ) -> Any:
        """Run an EOS command directly via exec_command (no FastCli/Cli wrapper)."""
        try:
            _, stdout, stderr = client.exec_command(inner, timeout=200)
            raw = ((stdout.read() or b"") + (stderr.read() or b"")).decode(
                "utf-8", errors="replace"
            )
        except Exception as e:
            logger.debug("Arista SSH %s direct: %s", self.ip, e)
            return {} if as_json else {"output": ""}
        if as_json:
            p = _arista_parse_json_block(raw)
            return p if p is not None else {}
        return {"output": raw}

    def _ssh_run_one(
        self, client: Any, rlock: threading.RLock, inner: str, as_json: bool
    ) -> Any:
        with rlock:
            # Fast path: already confirmed this device uses a direct EOS shell
            if self._ssh_is_direct_eos():
                return self._ssh_run_direct(client, rlock, inner, as_json)

            for prefix in ("FastCli", "Cli"):
                try:
                    _, stdout, stderr = client.exec_command(
                        f"{prefix} -c {shlex.quote(inner)}", timeout=200
                    )
                    raw = ((stdout.read() or b"") + (stderr.read() or b"")).decode(
                        "utf-8", errors="replace"
                    )
                except Exception as e:
                    logger.debug("Arista SSH %s %s: %s", self.ip, prefix, e)
                    continue
                if as_json:
                    p = _arista_parse_json_block(raw)
                    if p is not None:
                        return p
                else:
                    return {"output": raw}

            # Fallback: EOS shell may be directly accessible (no FastCli/Cli available)
            result = self._ssh_run_direct(client, rlock, inner, as_json)
            got_data = bool(result) if as_json else bool(result.get("output", "").strip())
            if got_data:
                with _SSH_GLOCK:
                    ent = _SSH_POOL.get(self.device.id)
                    if ent:
                        ent["direct_eos"] = True
            return result

    def _arista_run_ssh(self, version: int, cmds: List[str], fmt: str) -> List[Any]:
        c, rlock = self._ensure_ssh_pooled()
        # eAPI runs a whole batch in one CLI session. FastCli -c is one shot; for text,
        # join configure-style sequences with ';' (EOS separator).
        if fmt == "text" and len(cmds) > 1:
            merged = " ; ".join(cmds)
            out = [self._ssh_run_one(c, rlock, merged, as_json=False)]
        else:
            out = []
            for com in cmds:
                inner = f"{com} | json" if fmt == "json" else com
                out.append(
                    self._ssh_run_one(c, rlock, inner, as_json=(fmt == "json"))
                )
        with _SSH_GLOCK:
            ent = _SSH_POOL.get(self.device.id)
            if ent:
                ent["last"] = time.time()
        return out

    def _ensure_arista_mode(self) -> None:
        if self._arista_mode is not None:
            return
        self.probe()

    def _arista_run(self, version: int, cmds: List[str], fmt: str) -> Any:
        """Run *cmds* via the probed transport; returns a list (one entry per command,
        or a single merged entry for multi-command text over SSH). Raises OSError when
        no transport works."""
        self._ensure_arista_mode()
        # For SSH transport override (user set transport='ssh' explicitly)
        if self.transport == 'ssh' and self._arista_mode != 'eapi':
            self._arista_mode = 'ssh'
        if self._arista_mode == "eapi":
            return self._get_switch().runCmds(self._eapi_schema_version(), cmds, fmt)
        if self._arista_mode == "ssh":
            return self._arista_run_ssh(version, cmds, fmt)
        raise OSError("Arista: no working transport; run probe() first")

    def _sonic_delegate(self, method_name, *args, **kwargs):
        """Delegate a method call to a SonicDriver instance using SONiC credentials."""
        try:
            from .sonic import SonicDriver

            class _SonicDevProxy:
                pass

            sonic_u, sonic_p = self._sonic_credentials()
            proxy = _SonicDevProxy()
            proxy.ip_address = self.ip
            proxy.username = sonic_u
            proxy.password = sonic_p
            proxy.transport = 'auto'
            proxy.api_port = getattr(self.device, 'api_port', None)
            proxy.id = self.device.id

            sonic_drv = SonicDriver(proxy)
            method = getattr(sonic_drv, method_name)
            return method(*args, **kwargs)
        except Exception as e:
            logger.debug("Arista→SONiC delegate %s: %s", method_name, e)
            return DriverResult(error=str(e))

    def _should_use_sonic(self) -> bool:
        """Return True if the box is currently running SONiC and we should delegate to SonicDriver."""
        return self._is_dual_os() and self._active_os == 'sonic'

    def _secondary_vendor(self) -> str:
        """Return vendor_type_secondary from api_key JSON or model field (e.g. 'sonic')."""
        v = (getattr(self.device, "vendor_type_secondary", "") or "").strip()
        if not v:
            v = self._eos_dict_opts().get("vendor_type_secondary", "") or ""
        return v.lower()

    def _sonic_credentials(self) -> Tuple[str, str]:
        """Return (username, password) to use when box is running SONiC."""
        u = (getattr(self.device, "sonic_username", "") or "").strip()
        p = (getattr(self.device, "sonic_password", "") or "").strip()
        if not u:
            u = self.username
        if not p:
            p = self.password
        return u, p

    def _eos_credentials(self) -> Tuple[str, str]:
        """Return (username, password) to use when box is running EOS."""
        u = (getattr(self.device, "arista_username", "") or "").strip()
        p = (getattr(self.device, "arista_password", "") or "").strip()
        if not u:
            u = self.username
        if not p:
            p = self.password
        return u, p

    def _probe_ssh_with_creds(self, ssh_user: str, ssh_pass: str) -> str:
        """Low-level SSH probe using explicit credentials. Returns 'ok', 'auth_failed', 'unreachable'."""
        try:
            import paramiko
            from paramiko.ssh_exception import AuthenticationException
        except ImportError:
            return "unreachable"
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(
                self.ip,
                port=self._ssh_port(),
                username=ssh_user,
                password=ssh_pass,
                timeout=8,
                allow_agent=False,
                look_for_keys=False,
            )
        except AuthenticationException:
            return "auth_failed"
        except Exception:
            return "unreachable"

        # Run show version to determine OS
        result = "ok"
        try:
            _, stdout, _ = c.exec_command("show version", timeout=12)
            out = (stdout.read() or b"").decode("utf-8", errors="replace").lower()
            if "sonic" in out or "buildcommit" in out or "distribution" in out:
                result = "sonic"
            elif "arista" in out or "eos" in out or "dcs-" in out:
                result = "eos"
            else:
                # Try FastCli for EOS detection
                try:
                    _, s2, _ = c.exec_command("FastCli -c 'show version'", timeout=12)
                    out2 = (s2.read() or b"").decode("utf-8", errors="replace").lower()
                    if any(x in out2 for x in ("arista", "eos", "dcs-")):
                        result = "eos"
                    elif "sonic" in out2:
                        result = "sonic"
                except Exception:
                    pass
        except Exception:
            pass
        c.close()
        return result

    def detect_active_os(self) -> str:
        """
        Probe the box to detect whether it is currently running EOS or SONiC.
        Returns 'eos', 'sonic', or 'unknown'.
        Caches result in self._active_os.
        Only meaningful when vendor_type_secondary is set.
        """
        if self._active_os is not None:
            return self._active_os

        if not self._secondary_vendor():
            self._active_os = "eos"
            return "eos"

        eos_u, eos_p = self._eos_credentials()
        sonic_u, sonic_p = self._sonic_credentials()

        # Try EOS credentials first (FastCli probe)
        r_eos = self._probe_ssh_with_creds(eos_u, eos_p)
        if r_eos == "eos":
            self._active_os = "eos"
            return "eos"
        if r_eos == "sonic":
            self._active_os = "sonic"
            return "sonic"

        # Try SONiC credentials
        r_sonic = self._probe_ssh_with_creds(sonic_u, sonic_p)
        if r_sonic == "sonic":
            self._active_os = "sonic"
            return "sonic"
        if r_sonic == "eos":
            self._active_os = "eos"
            return "eos"

        # eAPI success implies EOS
        if self._probe_proto(self.transport) == "ok":
            self._active_os = "eos"
            return "eos"

        self._active_os = "unknown"
        return "unknown"

    def _is_dual_os(self) -> bool:
        """Return True if this device has dual-OS rotation enabled (dual_os_mode field or secondary vendor)."""
        return bool(getattr(self.device, 'dual_os_mode', False) or self._secondary_vendor())

    def probe(self) -> str:
        """Probe the device: for dual-OS (EOS/SONiC) boxes, auto-detect active OS first
        so the correct credentials are used for subsequent SSH sessions.

        Tries each connect target in order; per target: eAPI https → http (unless
        ``transport``/``prefer_ssh`` say otherwise), then SSH. Returns ``'ok'``,
        ``'auth_failed'`` or ``'unreachable'`` and sets ``_arista_mode``.
        """
        if len(self._connect_targets) > 1:
            saved_ip = self.ip
            last = 'unreachable'
            for target in self.iter_connect_targets():
                self.ip = target
                last = self._probe_single_address()
                if last == 'ok':
                    return 'ok'
            self.ip = saved_ip
            return last
        return self._probe_single_address()

    def _probe_single_address(self) -> str:
        # Seed _active_os from last known state in api_key (avoids double SSH on every probe)
        if self._active_os is None and self._is_dual_os():
            try:
                import json as _js
                api = _js.loads(getattr(self.device, 'api_key', '') or '{}')
                cached_os = api.get('active_os_detected', '')
                if cached_os in ('eos', 'sonic'):
                    self._active_os = cached_os
            except Exception:
                pass

        # For dual-OS boxes, detect OS first so _active_credentials() returns the right creds
        if self._is_dual_os() and self._active_os is None:
            eos_u, eos_p = self._eos_credentials()
            sonic_u, sonic_p = self._sonic_credentials()
            # Try EOS credentials first
            r_eos = self._probe_ssh_with_creds(eos_u, eos_p)
            if r_eos in ('eos', 'sonic', 'ok'):
                self._active_os = r_eos if r_eos in ('eos', 'sonic') else 'eos'
            elif sonic_u != eos_u or sonic_p != eos_p:
                # Try SONiC credentials
                r_sonic = self._probe_ssh_with_creds(sonic_u, sonic_p)
                if r_sonic in ('eos', 'sonic', 'ok'):
                    self._active_os = r_sonic if r_sonic in ('eos', 'sonic') else 'sonic'
            if self._active_os is None:
                self._active_os = 'unknown'
            logger.info("Arista dual-OS %s: detected active_os=%s", self.ip, self._active_os)

        # transport='ssh' forces SSH-only, skipping eAPI entirely
        if self.transport == 'ssh' or self._eos_dict_opts().get("prefer_ssh") is True:
            s = self._probe_ssh()
            if s == "ok":
                self._arista_mode = "ssh"
                return "ok"
            if s == "auth_failed":
                self._arista_mode = "ssh"
                return "auth_failed"
            return "unreachable"
        eapi_auth: Optional[bool] = None
        if self._active_os != 'sonic' or not self._is_dual_os():  # SONiC doesn't have eAPI
            if self.transport in ("http", "https"):
                r = self._probe_proto(self.transport)
                if r == "ok":
                    self._arista_mode = "eapi"
                    return "ok"
                if r == "auth_failed":
                    eapi_auth = True
            else:
                for proto in ("https", "http"):
                    r = self._probe_proto(proto)
                    if r == "ok":
                        self._arista_mode = "eapi"
                        return "ok"
                    if r == "auth_failed":
                        eapi_auth = True
        s2 = self._probe_ssh()
        if s2 == "ok":
            self._arista_mode = "ssh"
            return "ok"
        if s2 == "auth_failed":
            self._arista_mode = "ssh"
            return "auth_failed"
        if eapi_auth:
            return "auth_failed"
        return "unreachable"

    def get_lldp_neighbors_ssh(self) -> 'DriverResult':
        """Fetch LLDP neighbors via raw SSH — works for both EOS and SONiC, bypassing REST.
        Returns same format as get_lldp_neighbors().
        """
        try:
            ssh_user, ssh_pass = self._active_credentials()
            import paramiko
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                self.ip, port=self._ssh_port(),
                username=ssh_user, password=ssh_pass,
                timeout=12, look_for_keys=False, allow_agent=False,
            )
            # Detect OS from banner / show version if not yet known
            active_os = self._active_os or 'eos'

            def _run(cmd):
                _, out, _ = client.exec_command(cmd, timeout=20)
                return (out.read() or b'').decode('utf-8', errors='replace')

            neighbors = []
            if active_os == 'sonic':
                # SONiC: show lldp table (plain text, space-separated columns)
                raw = _run('show lldp table')
                if not raw.strip() or 'LocalPort' not in raw:
                    raw = _run('sudo lldpctl -f plain 2>/dev/null || show lldp neighbors')
                # Parse: LocalPort  RemoteDevice  RemotePortID  ...
                import re as _re
                for line in raw.splitlines():
                    line = line.strip()
                    if not line or line.startswith(('-','L','C','N','W')):
                        continue
                    parts = _re.split(r'\s{2,}', line)
                    if len(parts) >= 3 and parts[0].startswith('Ethernet'):
                        neighbors.append({
                            'local_port': parts[0],
                            'remote_device': parts[1].split('.')[0],
                            'remote_port': parts[2],
                        })
            else:
                # EOS: try FastCli 'show lldp neighbors | json' first
                raw = _run("FastCli -c 'show lldp neighbors | json'")
                if raw and '{' in raw:
                    try:
                        import json as _json
                        data = _json.loads(raw[raw.index('{'):raw.rindex('}')+1])
                        nlist = _normalize_lldp_neighbors_summary(data)
                        for n in nlist:
                            neighbors.append({
                                'local_port': n.get('port', ''),
                                'remote_device': n.get('neighborDevice', ''),
                                'remote_port': n.get('neighborPort', ''),
                            })
                    except Exception:
                        pass
                if not neighbors:
                    # Text fallback
                    raw = _run("FastCli -c 'show lldp neighbors'")
                    import re as _re
                    for line in raw.splitlines():
                        parts = _re.split(r'\s{2,}', line.strip())
                        if len(parts) >= 3 and parts[0].startswith(('Et', 'Ma', 'Ethernet')):
                            neighbors.append({
                                'local_port': parts[0],
                                'remote_device': parts[1],
                                'remote_port': parts[2] if len(parts) > 2 else '',
                            })
            client.close()
            return DriverResult(success=True, data=neighbors, error='')
        except Exception as e:
            logger.debug("Arista SSH LLDP %s: %s", self.ip, e)
            return DriverResult(error=str(e))

    def _get_switch(self):
        # Credentials are embedded in the jsonrpclib URL; do not log this object or its URL.
        proto = self._detect_protocol() or 'http'
        port = self.api_port or (443 if proto == 'https' else 80)
        return Server(f"{proto}://{self.username}:{self.password}@{self.ip}:{port}/command-api")

    def get_system_info(self):
        """``show version`` (+ text parse / ``show hostname`` fallbacks).

        ``data``: hostname, version, serial_number, model_name, mac_address, uptime,
        vendor_detail, active_os, secondary_os, dual_os.
        """
        if self._should_use_sonic():
            result = self._sonic_delegate('get_system_info')
            if result.success:
                d = result.data or {}
                d['active_os'] = 'sonic'
                d['dual_os'] = True
                d['secondary_os'] = 'eos'
                return DriverResult(success=True, data=d)
        try:
            resp = self._arista_run(1, ["show version"], "json")
            vd = (resp[0] if resp else None) or {}
            if not isinstance(vd, dict):
                vd = {}
            if not (str(vd.get("modelName", "")).strip() and str(vd.get("version", "")).strip()):
                tresp = self._arista_run(1, ["show version"], "text")
                t = (tresp[0] or {}).get("output", "") or ""
                for k, v in parse_show_version_text(t).items():
                    if v and not str(vd.get(k, "")).strip():
                        vd[k] = v

            # Supplement hostname from `show hostname | json` when version output lacks it
            hostname = str(vd.get("hostname", "")).strip()
            if not hostname or hostname == self.ip:
                try:
                    hr = self._arista_run(1, ["show hostname"], "json")
                    hd = (hr[0] if hr else None) or {}
                    if isinstance(hd, dict) and hd.get("hostname"):
                        hostname = str(hd["hostname"]).strip()
                except Exception:
                    pass
            if not hostname:
                hostname = self.ip

            active_os = self._active_os or "eos"
            secondary = self._secondary_vendor()
            version = str(vd.get("version", "")).strip()
            return DriverResult(success=True, data={
                'hostname': hostname,
                'version': version,
                'serial_number': vd.get('serialNumber', ''),
                'model_name': vd.get('modelName', ''),
                'mac_address': vd.get('systemMacAddress', ''),
                'uptime': vd.get('uptime', None),
                'vendor_detail': f"EOS {version}" if version else "EOS",
                'active_os': active_os,
                'secondary_os': secondary,
                'dual_os': bool(secondary),
            })
        except Exception as e:
            return DriverResult(error=str(e))

    def get_interfaces(self):
        """``show interfaces`` → physical/vlan/port_channel/management lists.

        Breakout lanes (``EthernetN/M``) are grouped under a synthetic parent row with
        ``is_group=True`` and ``children=[...]``.
        """
        if self._should_use_sonic():
            return self._sonic_delegate('get_interfaces')
        try:
            resp = self._arista_run(1, ["show interfaces"], "json")
            r0 = (resp[0] if resp else None) or {}
            if not isinstance(r0, dict):
                return DriverResult(error="Invalid show interfaces response")
            ifaces = r0.get("interfaces") or {}
            if not isinstance(ifaces, dict):
                return DriverResult(error="show interfaces: no interfaces object")
            interfaces_list = list(ifaces.items())
            physical, vlans, portchannels, mgmt = [], [], [], []

            raw_physical = []
            for name, info in interfaces_list:
                link = info.get('lineProtocolStatus', 'N/A')
                admin = info.get('interfaceStatus', 'N/A')
                bw = info.get('bandwidth', 0)
                intf = {
                    'name': name,
                    'short_name': self._short_name(name),
                    'status': link, 'admin_status': admin,
                    'description': info.get('description', ''),
                    'status_color': self._status_color(link, admin),
                    'bandwidth': bw, 'speed_label': self._speed_label(bw),
                    'mtu': info.get('mtu', ''),
                    'mac': info.get('physicalAddress', ''),
                }
                if 'Vlan' in name:
                    vlans.append(intf)
                elif 'Port-Channel' in name:
                    portchannels.append(intf)
                elif 'Management' in name:
                    mgmt.append(intf)
                else:
                    raw_physical.append(intf)

            def _sort_key(i):
                m = re.match(r'Ethernet(\d+)(?:/(\d+))?', i['name'])
                return (int(m.group(1)), int(m.group(2) or 0)) if m else (9999, 0)
            raw_physical.sort(key=_sort_key)

            port_groups = {}
            standalone = []
            for intf in raw_physical:
                m = re.match(r'Ethernet(\d+)/(\d+)', intf['name'])
                if m:
                    parent = int(m.group(1))
                    if parent not in port_groups:
                        port_groups[parent] = []
                    port_groups[parent].append(intf)
                else:
                    standalone.append(intf)

            for intf in standalone:
                m = re.match(r'Ethernet(\d+)$', intf['name'])
                port_num = int(m.group(1)) if m else 9999
                if port_num in port_groups:
                    intf['is_group'] = True
                    intf['children'] = port_groups.pop(port_num)
                else:
                    intf['is_group'] = False
                    intf['children'] = []
                physical.append(intf)

            for parent_num in sorted(port_groups.keys()):
                children = port_groups[parent_num]
                first = children[0]
                any_up = any(c['status'] == 'up' for c in children)
                all_admin_down = all(c['admin_status'] in ('disabled', 'adminDown') for c in children)
                if any_up:
                    group_color = 'green'
                elif all_admin_down:
                    group_color = 'yellow'
                else:
                    group_color = 'red'
                total_bw = sum(c['bandwidth'] for c in children)
                # If all lanes share the same speed and there are multiple lanes, show NxXXG
                lane_speeds = list({c['speed_label'] for c in children if c['speed_label']})
                if len(children) > 1 and len(lane_speeds) == 1:
                    group_speed = f'{len(children)}×{lane_speeds[0]}'
                elif total_bw:
                    group_speed = self._speed_label(total_bw)
                else:
                    group_speed = f'{len(children)}×{first["speed_label"]}'
                group = {
                    'name': f'Ethernet{parent_num}',
                    'short_name': f'Et{parent_num}',
                    'status': 'up' if any_up else 'down',
                    'admin_status': first['admin_status'],
                    'description': f'{len(children)}× breakout',
                    'status_color': group_color,
                    'bandwidth': total_bw,
                    'speed_label': group_speed,
                    'mtu': first['mtu'],
                    'mac': first['mac'],
                    'is_group': True,
                    'children': children,
                }
                physical.append(group)

            physical.sort(key=_sort_key)

            return DriverResult(success=True, data={
                'physical_data': physical, 'vlan_data': vlans,
                'port_channel_data': portchannels, 'management_data': mgmt,
            })
        except Exception as e:
            return DriverResult(error=str(e))

    def get_health(self):
        """``show version`` + ``show processes top once`` → cpu/memory/uptime dict."""
        if self._should_use_sonic():
            return self._sonic_delegate('get_health')
        try:
            resp = self._arista_run(1, ["show version", "show processes top once"], "json")
            vd = (resp[0] if resp else None) or {}
            if not isinstance(vd, dict):
                vd = {}
            mem_total = vd.get('memTotal', 0) or 0
            mem_free = vd.get('memFree', 0) or 0
            mem_used = mem_total - mem_free if mem_total else 0
            cpu_info = resp[1] if len(resp) > 1 else {}
            cpu_idle = 100.0
            if isinstance(cpu_info, dict):
                nested = cpu_info.get("cpuInfo", {})
                if isinstance(nested, dict):
                    cpu_idle = _cpu_idle_from_top_json(nested)
                else:
                    cpu_idle = _cpu_idle_from_top_json(cpu_info)
            cpu_util = round(100.0 - float(cpu_idle), 1)
            return DriverResult(success=True, data={
                'cpu_utilization': cpu_util,
                'memory_used': mem_used, 'memory_total': mem_total,
                'memory_percent': round(mem_used / mem_total * 100, 1) if mem_total else 0,
                'uptime': vd.get('uptime', 0), 'temperature': None,
            })
        except Exception as e:
            logger.debug("Arista get_health batch: %s", e)
            try:
                resp2 = self._arista_run(1, ["show version"], "json")
                vd = (resp2[0] if resp2 else None) or {}
                if not isinstance(vd, dict):
                    vd = {}
                mem_total = vd.get('memTotal', 0) or 0
                mem_free = vd.get('memFree', 0) or 0
                mem_used = mem_total - mem_free if mem_total else 0
                cpu_util = 0.0
                try:
                    top = self._arista_run(1, ["show processes top once"], "json")
                    tc = (top[0] if top else None) or {}
                    if isinstance(tc, dict):
                        nest = tc.get("cpuInfo", tc)
                        if isinstance(nest, dict):
                            cpu_idle = _cpu_idle_from_top_json(nest)
                            cpu_util = round(100.0 - float(cpu_idle), 1)
                except Exception as e2:
                    logger.debug("Arista get_health top fallback: %s", e2)
                return DriverResult(success=True, data={
                    'cpu_utilization': cpu_util,
                    'memory_used': mem_used, 'memory_total': mem_total,
                    'memory_percent': round(mem_used / mem_total * 100, 1) if mem_total else 0,
                    'uptime': vd.get('uptime', 0), 'temperature': None,
                })
            except Exception as e3:
                return DriverResult(error=str(e3))

    def get_routes(self):
        try:
            resp = self._arista_run(1, ["show ip route"], "json")
            routes = []
            for vrf_name, vrf_data in resp[0].get('vrfs', {}).items():
                for prefix, ri in vrf_data.get('routes', {}).items():
                    for via in ri.get('vias', [{}]):
                        routes.append({
                            'vrf': vrf_name, 'prefix': prefix,
                            'protocol': ri.get('routeType', 'unknown'),
                            'next_hop': via.get('nexthopAddr', 'directly connected'),
                            'interface': via.get('interface', ''),
                            'metric': ri.get('metric', ''),
                            'preference': ri.get('preference', ''),
                        })
            return DriverResult(success=True, data=routes)
        except Exception as e:
            return DriverResult(error=str(e))

    def get_vlans(self):
        try:
            resp = self._arista_run(1, ["show vlan"], "json")
            vlans = []
            for vid, vi in resp[0].get('vlans', {}).items():
                vlans.append({
                    'id': vid, 'name': vi.get('name', ''),
                    'status': vi.get('status', ''),
                    'interfaces': ', '.join(vi.get('interfaces', {}).keys()),
                })
            return DriverResult(success=True, data=vlans)
        except Exception as e:
            return DriverResult(error=str(e))

    def get_running_config(self):
        try:
            resp = self._arista_run(1, ["show running-config"], "text")
            return DriverResult(success=True, data=resp[0].get('output', ''))
        except Exception as e:
            return DriverResult(error=str(e))

    def get_startup_config(self):
        try:
            resp = self._arista_run(1, ["show startup-config"], "text")
            return DriverResult(success=True, data=resp[0].get('output', ''))
        except Exception as e:
            return DriverResult(error=str(e))

    def execute_command(self, command):
        """Run one ``show ...`` command as text; anything else is rejected.

        The check is a prefix match only; over SSH the string is passed to
        ``FastCli -c`` (or directly to the login shell on direct-EOS sessions).
        """
        cmd = command.strip()
        if not cmd.lower().startswith('show'):
            return DriverResult(error="Only 'show' commands are allowed.")
        try:
            resp = self._arista_run(1, [cmd], "text")
            return DriverResult(success=True, data=resp[0].get('output', ''))
        except Exception as e:
            return DriverResult(error=str(e))

    def get_arp_table(self):
        try:
            resp = self._arista_run(1, ["show arp"], "json")
            entries = []
            for e in resp[0].get('ipV4Neighbors', []):
                entries.append({'ip': e.get('address', ''), 'mac': e.get('hwAddress', ''),
                               'interface': e.get('interface', ''), 'age': e.get('age', '')})
            return DriverResult(success=True, data=entries)
        except Exception as e:
            return DriverResult(error=str(e))

    def get_mac_table(self):
        try:
            resp = self._arista_run(1, ["show mac address-table"], "json")
            entries = []
            for e in resp[0].get('unicastTable', {}).get('tableEntries', []):
                entries.append({'mac': e.get('macAddress', ''), 'vlan': e.get('vlanId', ''),
                               'interface': e.get('interface', ''), 'type': e.get('entryType', '')})
            return DriverResult(success=True, data=entries)
        except Exception as e:
            return DriverResult(error=str(e))

    def get_lldp_neighbors(self):
        if self._should_use_sonic():
            # Try SONiC SSH LLDP first (most reliable for these boxes)
            result = self.get_lldp_neighbors_ssh()
            if result.success and result.data:
                return result
            return self._sonic_delegate('get_lldp_neighbors')
        try:
            resp = self._arista_run(1, ["show lldp neighbors"], "json")
            r0 = (resp[0] if resp else None) or {}
            if not isinstance(r0, dict):
                r0 = {}
            nlist = _normalize_lldp_neighbors_summary(r0)
            neighbors = []
            for n in nlist:
                neighbors.append({'local_port': n.get('port', ''),
                                 'remote_device': n.get('neighborDevice', ''),
                                 'remote_port': n.get('neighborPort', '')})
            return DriverResult(success=True, data=neighbors)
        except Exception as e:
            return DriverResult(error=str(e))

    def get_lldp_neighbors_detail(self):
        """Fetch detailed LLDP info with chassis-id, management IPs, system-name."""
        try:
            resp = self._arista_run(1, ["show lldp neighbors detail"], "json")
            neighbors = []
            for intf_name, intf_data in resp[0].get('lldpNeighbors', {}).items():
                for n in intf_data.get('lldpNeighborInfo', []):
                    mgmt_addrs = n.get('managementAddresses', [])
                    mgmt_ip = mgmt_addrs[0].get('address', '') if mgmt_addrs else ''
                    nif = n.get('neighborInterfaceInfo', {})
                    remote_port = (
                        nif.get('interfaceId_v2', '')
                        or nif.get('interfaceDescription', '')
                        or nif.get('interfaceId', '')
                        or n.get('portId', '')
                    )
                    remote_port = remote_port.strip('"').strip("'")
                    neighbors.append({
                        'local_port': intf_name,
                        'remote_device': n.get('systemName', ''),
                        'remote_port': remote_port,
                        'chassis_id': n.get('chassisId', ''),
                        'mgmt_ip': mgmt_ip,
                        'system_description': n.get('systemDescription', ''),
                    })
            return DriverResult(success=True, data=neighbors)
        except Exception as e:
            return DriverResult(error=str(e))

    # ---- Enhanced driver methods ----

    def get_bgp_summary(self):
        try:
            resp = self._arista_run(1, ["show ip bgp summary"], "json")
            peers = []
            for vrf_name, vrf_data in resp[0].get('vrfs', {}).items():
                for peer_addr, peer_info in vrf_data.get('peers', {}).items():
                    peers.append({
                        'neighbor': peer_addr,
                        'asn': peer_info.get('asn', ''),
                        'state': peer_info.get('peerState', 'unknown'),
                        'prefixes_received': peer_info.get('prefixReceived', 0),
                        'uptime': peer_info.get('upDownTime', ''),
                        'vrf': vrf_name,
                    })
            return DriverResult(success=True, data=peers)
        except Exception as e:
            return DriverResult(error=str(e))

    def get_ospf_neighbors(self):
        try:
            resp = self._arista_run(1, ["show ip ospf neighbor"], "json")
            neighbors = []
            for vrf_name, vrf_data in resp[0].get('vrfs', {}).items():
                for inst_id, inst_data in vrf_data.get('instList', {}).items():
                    for nbr_id, nbr_data in inst_data.get('ospfNeighborEntries', {}).items():
                        for entry in nbr_data if isinstance(nbr_data, list) else [nbr_data]:
                            neighbors.append({
                                'neighbor_id': entry.get('routerId', nbr_id),
                                'address': entry.get('interfaceAddress', ''),
                                'state': entry.get('adjacencyState', ''),
                                'interface': entry.get('interfaceName', ''),
                                'area': entry.get('areaId', ''),
                                'priority': entry.get('priority', ''),
                            })
            return DriverResult(success=True, data=neighbors)
        except Exception as e:
            return DriverResult(error=str(e))

    def get_environment(self):
        try:
            resp = self._arista_run(1, ["show environment all"], "json")
            env = resp[0] if resp else {}
            sensors = []
            fans = []
            psus = []
            # Temperature sensors
            for name, info in env.get('temperatureInfo', {}).items():
                sensors.append({
                    'name': name,
                    'value': f"{info.get('currentTemperature', 'N/A')}°C",
                    'type': 'temperature',
                    'status': 'ok' if info.get('alertStatus', '') == 'ok' else info.get('alertStatus', 'unknown'),
                })
            # Fans
            for name, info in env.get('fanTraySlots', {}).items():
                for fan_name, fan_info in info.get('fans', {}).items():
                    fans.append({
                        'name': f"{name}/{fan_name}",
                        'status': fan_info.get('status', 'unknown'),
                        'speed': fan_info.get('currentSpeed', ''),
                    })
            # Power supplies (key name varies by platform / EOS)
            ps_map = env.get('powerSupplySlots') or env.get('powerSupplies') or {}
            if isinstance(ps_map, dict):
                for name, info in ps_map.items():
                    if not isinstance(info, dict):
                        continue
                    psus.append({
                        'name': name,
                        'status': info.get('state', info.get('status', 'unknown')),
                        'model': info.get('modelName', ''),
                        'watts': info.get('outputPower', ''),
                    })
            return DriverResult(success=True, data={
                'sensors': sensors, 'fans': fans, 'power_supplies': psus,
            })
        except Exception as e:
            return DriverResult(error=str(e))

    def get_interface_counters(self):
        try:
            resp = self._arista_run(1, ["show interfaces counters"], "json")
            counters_by_name: Dict[str, dict] = {}
            for name, info in (resp[0].get('interfaces') or {}).items():
                counters_by_name[name] = {
                    'name': name,
                    'bytes_in': info.get('inOctets', 0),
                    'bytes_out': info.get('outOctets', 0),
                    'packets_in': info.get('inUcastPkts', 0) + info.get('inMulticastPkts', 0),
                    'packets_out': info.get('outUcastPkts', 0) + info.get('outMulticastPkts', 0),
                    'errors_in': info.get('inErrors', 0),
                    'errors_out': info.get('outErrors', 0),
                }
            try:
                resp_if = self._arista_run(1, ["show interfaces"], "json")
                for name, info in (resp_if[0].get('interfaces') or {}).items():
                    if not is_physical_ethernet_iface(name):
                        continue
                    in_disc = extract_input_discards_from_interface_info(info)
                    row = counters_by_name.get(name)
                    if row is not None:
                        row['input_discards'] = in_disc
                    else:
                        counters_by_name[name] = {
                            'name': name,
                            'bytes_in': 0,
                            'bytes_out': 0,
                            'packets_in': 0,
                            'packets_out': 0,
                            'errors_in': 0,
                            'errors_out': 0,
                            'input_discards': in_disc,
                        }
            except Exception as disc_exc:
                logger.debug('merge input discards from show interfaces failed: %s', disc_exc)
            return DriverResult(success=True, data=list(counters_by_name.values()))
        except Exception as e:
            return DriverResult(error=str(e))

    def get_port_channel_members(self):
        """Fetch LAG members using 'show port-channel dense' (has ALL ports with status)
        and fall back to 'show port-channel' (active/inactive split)."""
        try:
            result = {}
            # Primary: 'show port-channel dense' has 'ports' dict with all members
            try:
                resp = self._arista_run(1, ["show port-channel dense"], "json")
                for pc_name, pc_info in resp[0].get('portChannels', {}).items():
                    ports = pc_info.get('ports', {})
                    if ports:
                        result[pc_name] = list(ports.keys())
                    else:
                        # Fallback within dense: check activePorts/inactivePorts
                        members = list(pc_info.get('activePorts', {}).keys())
                        members += list(pc_info.get('inactivePorts', {}).keys())
                        result[pc_name] = members
            except Exception:
                pass

            # If dense didn't return members, try 'show port-channel'
            if not result or all(len(v) == 0 for v in result.values()):
                resp = self._arista_run(1, ["show port-channel"], "json")
                for pc_name, pc_info in resp[0].get('portChannels', {}).items():
                    members = list(pc_info.get('activePorts', {}).keys())
                    members += list(pc_info.get('inactivePorts', {}).keys())
                    if members:
                        result[pc_name] = members

            return DriverResult(success=True, data=result)
        except Exception as e:
            return DriverResult(error=str(e))

    def get_dom_info(self):
        try:
            resp = self._arista_run(1, ["show interfaces transceiver"], "json")
            dom_list = []
            for name, info in resp[0].get('interfaces', {}).items():
                dom_list.append({
                    'interface': name,
                    'media_type': info.get('mediaType', ''),
                    'vendor': info.get('vendorSn', ''),
                    'rx_power': info.get('rxPower', ''),
                    'tx_power': info.get('txPower', ''),
                    'temperature': info.get('temperature', ''),
                    'voltage': info.get('voltage', ''),
                })
            return DriverResult(success=True, data=dom_list)
        except Exception as e:
            return DriverResult(error=str(e))

    # ---- Configuration Push ----

    def send_config(self, commands, commit=True):
        """Push config commands via eAPI configure mode (or ``;``-joined FastCli over SSH).

        Wraps *commands* in ``enable`` / ``configure`` / ``end`` and appends
        ``write memory`` when *commit*. ``data``: {outputs, commands_sent}.
        """
        try:
            cmds = ['enable', 'configure'] + list(commands) + ['end']
            if commit:
                cmds.append('write memory')
            resp = self._arista_run(1, cmds, 'text')
            outputs = [r.get('output', '') for r in resp]
            return DriverResult(success=True, data={
                'outputs': outputs,
                'commands_sent': len(commands),
            })
        except Exception as e:
            return DriverResult(error=str(e))

    def _eapi_command_api_reachable(self) -> bool:
        """Return True if HTTP/HTTPS eAPI answers with 200 to ``runCmds`` (does not set ``_arista_mode``)."""
        if self.transport in ("http", "https"):
            return self._probe_proto(self.transport) == "ok"
        for proto in ("https", "http"):
            if self._probe_proto(proto) == "ok":
                return True
        return False

    def promote_to_eapi(self) -> DriverResult:
        """Enable ``management api http-commands`` over SSH, then re-probe so a later refresh can use eAPI.

        Idempotent: if eAPI is already reachable with the stored credentials, returns without changing config.
        For switches that require the VRF name under ``management api http-commands``, set
        ``api_key`` key ``eapi_vrf`` (e.g. ``"management"``). Requires SSH to push new config when
        eAPI is not yet up.
        """
        from django.core.cache import cache

        self._ensure_arista_mode()
        if self._eapi_command_api_reachable():
            return DriverResult(
                success=True,
                data={"message": "eAPI already reachable on management with current credentials."},
            )
        if self._arista_mode != "ssh":
            return DriverResult(
                error=(
                    "Cannot push eAPI config: need an SSH session to the switch (eAPI is not up yet "
                    "or credentials failed)."
                )
            )
        opts = self._eos_dict_opts()
        cooldown_key = f"arista_eapi_config_push_dedup_{self.device.id}"
        force_retry = bool(opts.get("eapi_config_force_retry"))
        if force_retry:
            try:
                cache.delete(cooldown_key)
            except Exception:
                pass
        try:
            in_cooldown = bool(cache.get(cooldown_key)) and not force_retry
        except Exception:
            in_cooldown = False

        if in_cooldown:
            if self._eapi_command_api_reachable():
                clear_cache(self.ip)
                _arista_ssh_pool_close_device(self.device.id)
                self._arista_mode = None
                time.sleep(0.5)
                p = self.probe()
                if p == "ok" and self._arista_mode == "eapi":
                    return DriverResult(
                        success=True,
                        data={"message": "eAPI is now up; driver re-probed to eAPI."},
                    )
                if p == "ok" and self._eapi_command_api_reachable():
                    return DriverResult(
                        success=True,
                        data={
                            "message": (
                                "eAPI is up; driver still on SSH (prefer_ssh). Remove prefer_ssh in "
                                "api_key to use eAPI for future refreshes."
                            )
                        },
                    )
            return DriverResult(
                success=True,
                data={
                    "message": (
                        "eAPI not up yet; config push throttled to once per hour. "
                        "Set eapi_config_force_retry in api_key to push again, or check VRF/ACL."
                    )
                },
            )

        lines = ["management api http-commands"]
        vrf = opts.get("eapi_vrf")
        if isinstance(vrf, str) and vrf.strip():
            lines.append("vrf %s" % vrf.strip())
        lines.extend(
            [
                "protocol http https",
                "no shutdown",
            ]
        )
        push = self.send_config(lines, commit=True)
        if not push.success:
            return push
        try:
            cache.set(cooldown_key, 1, 3600)
        except Exception:
            pass
        clear_cache(self.ip)
        _arista_ssh_pool_close_device(self.device.id)
        self._arista_mode = None
        time.sleep(1.5)
        if not self._eapi_command_api_reachable():
            return DriverResult(
                success=True,
                data={
                    "message": (
                        "management api config pushed; eAPI is not yet accepting HTTP/HTTPS—check "
                        "VRF/ACL, or set api_key eapi_vrf if management is not in the default VRF."
                    )
                },
            )
        p = self.probe()
        if p == "ok" and self._arista_mode == "eapi":
            return DriverResult(
                success=True,
                data={"message": "eAPI enabled; driver re-probed and is using eAPI."},
            )
        return DriverResult(
            success=True,
            data={
                "message": (
                    "eAPI is reachable; probe=%r mode=%r (remove prefer_ssh to prefer eAPI when both work)."
                    % (p, self._arista_mode)
                )
            },
        )

    def enable_lldp(self):
        """Enable LLDP globally on Arista EOS."""
        try:
            cmds = [
                'enable',
                'configure',
                'lldp run',
                'end',
                'write memory',
            ]
            self._arista_run(1, cmds, 'text')
            # Verify
            resp = self._arista_run(1, ['show lldp'], 'text')
            output = resp[0].get('output', '')
            return DriverResult(success=True, data={
                'enabled_count': 1,
                'details': ['LLDP run enabled globally (TX/RX on all interfaces by default)'],
                'verify': output[:500],
            })
        except Exception as e:
            return DriverResult(error=str(e))
