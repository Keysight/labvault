"""Client for labvault-opsd Unix socket (no docker.sock)."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from connect.labvault_cli.service_catalog import LOGICAL_NAMES, SERVICES

SOCK = (
    os.environ.get("LABVAULT_OPS_SOCK")
    or os.environ.get("LABVAULT_OPS_SOCK")
    or "/run/labvault/ops.sock"
)


def _call(payload: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
    path = Path(SOCK)
    if not path.exists():
        return {"ok": False, "error": "opsd_unavailable", "state": "failed", "services": []}
    data = json.dumps(payload).encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall(data + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except OSError as exc:
        return {
            "ok": False,
            "error": "opsd_connect_failed",
            "detail": str(exc),
            "state": "failed",
        }
    try:
        return json.loads(buf.decode("utf-8"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad_opsd_response", "state": "failed"}


def list_services() -> list[dict[str, Any]]:
    resp = _call({"action": "list"})
    if resp.get("services"):
        return resp["services"]
    return [
        {"name": n, "state": "unknown", "notes": SERVICES[n].get("notes", "")}
        for n in LOGICAL_NAMES
    ]


def status_service(name: str) -> dict[str, Any]:
    if name != "all" and name not in SERVICES:
        return {"ok": False, "error": "component_not_allowlisted", "state": "denied"}
    return _call({"action": "status", "name": name})


def lifecycle(
    action: str,
    name: str,
    *,
    source: str,
    reason: str,
    request_id: str = "",
) -> dict[str, Any]:
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": "invalid_action", "state": "failed"}
    return _call(
        {
            "action": action,
            "name": name,
            "source": source,
            "reason": reason,
            "request_id": request_id,
        },
        timeout=120.0,
    )
