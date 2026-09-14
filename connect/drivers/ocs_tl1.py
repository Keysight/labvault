"""
TL1 cross-connect operations for Calient OCS (via SSH → localhost:3083).

Used when REST credentials are read-only (403 on write APIs).
Credentials come from ``Device.api_key`` JSON::

    {
      "ssh_user": "root",
      "ssh_password": "…",
      "tl1_user": "admin",
      "tl1_password": "…",
      "tl1_port": 3083
    }
"""
from __future__ import annotations

import logging
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from .base import DriverResult

logger = logging.getLogger(__name__)

_TL1_DENY = re.compile(r"\bDENY\b|\bDENIED\b|\bFAIL\b", re.I)
_TL1_OK = re.compile(r"\bCOMPLD\b", re.I)


def tl1_credentials_from_device(device) -> Optional[Dict[str, Any]]:
    """Return SSH/TL1 creds dict if device api_key JSON has enough fields."""
    import json

    raw = (getattr(device, "api_key", None) or "").strip()
    opts: Dict[str, Any] = {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                opts = parsed
        except json.JSONDecodeError:
            opts = {}

    tags = {t.strip().lower() for t in (getattr(device, "tags", None) or "").split(",") if t.strip()}
    ssh_user = (opts.get("ssh_user") or "").strip()
    ssh_password = (opts.get("ssh_password") or opts.get("ssh_pass") or "").strip()
    tl1_user = (opts.get("tl1_user") or "").strip()
    tl1_password = (opts.get("tl1_password") or opts.get("tl1_pass") or "").strip()

    if "ocs-lab" in tags:
        ssh_user = ssh_user or "root"
        ssh_password = ssh_password or "ixia123"
        tl1_user = tl1_user or "admin"
        tl1_password = tl1_password or "pxc***"

    if not (ssh_user and ssh_password and tl1_user and tl1_password):
        return None
    return {
        "ssh_user": ssh_user,
        "ssh_password": ssh_password,
        "tl1_user": tl1_user,
        "tl1_password": tl1_password,
        "tl1_port": int(opts.get("tl1_port") or 3083),
    }


def run_tl1_over_ssh(
    host: str,
    creds: Dict[str, Any],
    commands: List[str],
    *,
    timeout: int = 180,
) -> Tuple[bool, str]:
    """Execute TL1 commands on OCS via SSH + telnet to localhost TL1 port."""
    port = int(creds.get("tl1_port") or 3083)
    tl1_user = creds["tl1_user"]
    tl1_password = creds["tl1_password"]
    lines = [f"ACT-USER::{tl1_user}:::{tl1_password};"]
    lines.extend(commands)
    lines.append("CANC-USER:::;")

    remote_lines = ["(", "sleep 0.4"]
    for ln in lines:
        escaped = ln.replace("'", "'\"'\"'")
        remote_lines.append(f"echo '{escaped}'")
        remote_lines.append("sleep 0.7")
    remote_lines.append(f") | timeout {max(30, timeout - 10)} telnet 127.0.0.1 {port} 2>&1")
    remote = "\n".join(remote_lines)

    cmd = [
        "sshpass",
        "-p",
        creds["ssh_password"],
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=15",
        f"{creds['ssh_user']}@{host}",
        "bash",
        "-s",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=remote,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode not in (0, 124) and not out.strip():
        return False, f"SSH/TL1 failed (exit {proc.returncode})"
    if _TL1_DENY.search(out) and not _TL1_OK.search(out):
        return False, out[-2000:]
    if commands and not _TL1_OK.search(out):
        return False, out[-2000:] or "TL1 did not return COMPLD"
    return True, out[-4000:]


def restore_connections_via_tl1(
    host: str,
    creds: Dict[str, Any],
    connections: List[Dict[str, Any]],
    *,
    clear_first: bool = True,
) -> DriverResult:
    """Clear (optional) and replay cross-connects using ENT-CRS / ACT-CRS."""
    cmds: List[str] = []
    if clear_first:
        cmds.append("DLT-CRS-ALL:::;")
    for row in connections:
        a = str(row.get("in") or "").strip()
        b = str(row.get("out") or "").strip()
        if not a or not b:
            continue
        cmds.append(f"ENT-CRS::{a},{b}:::,2WAY;")
    for row in connections:
        a = str(row.get("in") or "").strip()
        b = str(row.get("out") or "").strip()
        if not a or not b:
            continue
        conn = str(row.get("conn") or f"{a}-{b}").strip()
        cmds.append(f"ACT-CRS::::{conn};")
    if not cmds:
        return DriverResult(success=False, error="No connections to restore")
    ok, detail = run_tl1_over_ssh(host, creds, cmds, timeout=max(120, 30 + len(cmds) * 2))
    if not ok:
        return DriverResult(success=False, error=detail or "TL1 restore failed", data={"method": "tl1"})
    return DriverResult(
        success=True,
        data={"method": "tl1", "commands": len(cmds), "connections": len(connections), "detail": detail},
    )


def is_rest_permission_denied(err: Optional[str]) -> bool:
    if not err:
        return False
    low = err.lower()
    return "403" in low or "forbidden" in low or "permission" in low or "do not have permission" in low
