#!/usr/bin/env python3
"""Least-privilege LabVault operations broker (Unix socket). No docker.sock."""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

from service_catalog import (
    ACTIONS,
    LOGICAL_NAMES,
    SERVICES,
    capability_allows,
    resolve_service_name,
    restart_all_targets,
)

SOCK = os.environ.get("LABVAULT_OPS_SOCK") or os.environ.get("LABVAULT_OPS_SOCK", "/run/labvault/ops.sock")
ADAPTER = os.environ.get("LABVAULT_OPS_ADAPTER", "auto")
COMPOSE_PROJECT_DIR = os.environ.get("LABVAULT_COMPOSE_PROJECT_DIR", "/opt/labvault/current")
COMPOSE_FILE = os.environ.get("LABVAULT_COMPOSE_FILE", "deploy/compose/docker-compose.yml")
COMPOSE_PROJECT_NAME = os.environ.get("LABVAULT_COMPOSE_PROJECT_NAME", "").strip()
COMMAND_TIMEOUT = float(os.environ.get("LABVAULT_OPS_TIMEOUT", "90"))

_LOCK = threading.Lock()
_COMPOSE_IDENTITY: dict[str, int] | None = None


def _log(msg: str) -> None:
    print(f"labvault-opsd: {msg}", flush=True)


def _peercred(conn: socket.socket) -> dict:
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, 17, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
        return {"pid": pid, "uid": uid, "gid": gid}
    except OSError:
        return {}


def _detect_adapter() -> str:
    if ADAPTER in ("systemd", "compose"):
        return ADAPTER
    compose_path = Path(COMPOSE_PROJECT_DIR) / COMPOSE_FILE
    if compose_path.is_file():
        try:
            if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
                return "compose"
        except FileNotFoundError:
            # Bare-metal / systemd installs have no docker binary.
            pass
    return "systemd"


def _validate_compose_tree() -> tuple[bool, str]:
    global _COMPOSE_IDENTITY
    root = Path(COMPOSE_PROJECT_DIR).resolve()
    compose = (root / COMPOSE_FILE).resolve()
    try:
        compose.relative_to(root)
    except ValueError:
        return False, "compose_path_escape"
    for path in (root, compose):
        if path.is_symlink():
            return False, f"symlink_rejected:{path}"
        st = path.stat()
        if st.st_uid != 0:
            return False, f"not_root_owned:{path}"
        if (st.st_mode & 0o777) & 0o022:
            return False, f"group_world_writable:{path}"
    env_file = root / ".env"
    if env_file.exists():
        if env_file.is_symlink():
            return False, "env_symlink_rejected"
        st = env_file.stat()
        if st.st_uid != 0:
            return False, "env_not_root_owned"
        if (st.st_mode & 0o777) & 0o022:
            return False, "env_group_world_writable"
    identity = {"dev": compose.stat().st_dev, "ino": compose.stat().st_ino}
    if _COMPOSE_IDENTITY is None:
        _COMPOSE_IDENTITY = identity
    elif _COMPOSE_IDENTITY != identity:
        return False, "compose_identity_changed"
    return True, "ok"


def _systemctl(*args: str) -> dict:
    try:
        r = subprocess.run(
            ["systemctl", *args], capture_output=True, text=True,
            timeout=COMMAND_TIMEOUT, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        )
        return {"rc": r.returncode, "stdout": (r.stdout or "")[-2000:], "stderr": (r.stderr or "")[-1000:]}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "error": "timeout", "state": "timeout"}
    except Exception as exc:
        return {"rc": 1, "error": str(exc), "state": "failed"}


def _compose(*args: str) -> dict:
    ok, reason = _validate_compose_tree()
    if not ok:
        return {"rc": 1, "error": reason, "state": "denied"}
    root = str(Path(COMPOSE_PROJECT_DIR).resolve())
    compose = str((Path(COMPOSE_PROJECT_DIR) / COMPOSE_FILE).resolve())
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/root"}
    dh = os.environ.get("DOCKER_HOST", "").strip()
    if dh:
        env["DOCKER_HOST"] = dh
    try:
        cmd = ["docker", "compose", "-f", compose, "--project-directory", root]
        if COMPOSE_PROJECT_NAME:
            cmd.extend(["-p", COMPOSE_PROJECT_NAME])
        cmd.extend(args)
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=COMMAND_TIMEOUT, cwd=root, env=env,
        )
        return {"rc": r.returncode, "stdout": (r.stdout or "")[-2000:], "stderr": (r.stderr or "")[-1000:]}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "error": "timeout", "state": "timeout"}
    except Exception as exc:
        return {"rc": 1, "error": str(exc), "state": "failed"}


def _status_one(adapter: str, name: str) -> dict:
    meta = SERVICES[name]
    # If the broker is answering, opsd itself is running regardless of unit name drift.
    if name == "opsd":
        units = meta.get("systemd_units") or ((meta.get("systemd_unit"),) if meta.get("systemd_unit") else ())
        active = None
        for unit in units:
            if not unit:
                continue
            st = _systemctl("is-active", unit)
            if st.get("rc") == 0:
                active = unit
                break
        return {
            "name": name,
            "ok": True,
            "state": "running",
            "unit": active or (units[0] if units else meta.get("systemd_unit")),
            "adapter": "host",
            "notes": meta.get("notes", ""),
        }
    if adapter == "compose":
        svc = meta.get("compose_service")
        if meta.get("host_only") or svc is None:
            units = list(meta.get("systemd_units") or ())
            unit = meta.get("systemd_unit")
            if unit and unit not in units:
                units.insert(0, unit)
            if not units:
                return {"name": name, "ok": True, "state": "status_only", "notes": meta.get("notes", "")}
            for u in units:
                st = _systemctl("is-active", u)
                if st.get("rc") == 0:
                    return {"name": name, "ok": True, "state": "running",
                            "unit": u, "adapter": "systemd", "notes": meta.get("notes", "")}
            return {"name": name, "ok": True, "state": "stopped",
                    "unit": units[0], "adapter": "systemd", "notes": meta.get("notes", "")}
        st = _compose("ps", "--status", "running", "--services")
        running = set((st.get("stdout") or "").split())
        return {"name": name, "ok": True, "state": "running" if svc in running else "stopped",
                "compose": svc, "adapter": "compose", "notes": meta.get("notes", "")}
    units = list(meta.get("systemd_units") or ())
    unit = meta.get("systemd_unit")
    if unit and unit not in units:
        units.insert(0, unit)
    if not units:
        return {"name": name, "ok": True, "state": "status_only", "notes": meta.get("notes", "")}
    for u in units:
        st = _systemctl("is-active", u)
        if st.get("rc") == 0:
            return {"name": name, "ok": True, "state": "running",
                    "unit": u, "adapter": "systemd", "notes": meta.get("notes", "")}
    return {"name": name, "ok": True, "state": "stopped",
            "unit": units[0], "adapter": "systemd", "notes": meta.get("notes", "")}


def _lifecycle_one(adapter: str, action: str, name: str, *, source: str) -> dict:
    meta = SERVICES[name]
    allowed, reason = capability_allows(meta, action, source=source)
    if not allowed:
        return {"ok": False, "name": name, "state": "denied", "error": reason}
    if adapter == "compose":
        svc = meta.get("compose_service")
        if svc is None:
            return {"ok": False, "name": name, "state": "unsupported", "error": "unsupported_on_compose"}
        cur = _status_one(adapter, name)
        if action == "start" and cur.get("state") == "running":
            return {"ok": True, "name": name, "state": "already_running"}
        if action == "stop" and cur.get("state") == "stopped":
            return {"ok": True, "name": name, "state": "already_stopped"}
        if action == "start":
            r = _compose("up", "-d", "--no-deps", svc)
        elif action == "stop":
            r = _compose("stop", svc)
        else:
            r = _compose("restart", svc)
        if r.get("state") in ("timeout", "denied"):
            return {"ok": False, "name": name, "state": r["state"], "detail": r}
        if r.get("rc", 1) != 0:
            return {"ok": False, "name": name, "state": "failed", "detail": r}
        final = _status_one(adapter, name)
        return {"ok": True, "name": name, "state": "ok", "final": final.get("state"), "result": r}
    unit = meta.get("systemd_unit")
    if not unit:
        return {"ok": False, "name": name, "state": "denied", "error": "status_only"}
    cur = _status_one(adapter, name)
    if action == "start" and cur.get("state") == "running":
        return {"ok": True, "name": name, "state": "already_running"}
    if action == "stop" and cur.get("state") == "stopped":
        return {"ok": True, "name": name, "state": "already_stopped"}
    r = _systemctl(action, unit)
    if r.get("state") == "timeout":
        return {"ok": False, "name": name, "state": "timeout", "detail": r}
    if r.get("rc", 1) != 0:
        return {"ok": False, "name": name, "state": "failed", "detail": r}
    final = _status_one(adapter, name)
    return {"ok": True, "name": name, "state": "ok", "final": final.get("state"), "result": r}


def handle(msg: dict, *, peer: dict | None = None) -> dict:
    if not isinstance(msg, dict):
        return {"ok": False, "error": "invalid_message", "state": "failed"}
    extra = set(msg) - {"action", "name", "source", "reason", "request_id"}
    if extra:
        return {"ok": False, "error": "extra_fields", "fields": sorted(extra), "state": "denied"}
    action = msg.get("action")
    if action not in ACTIONS:
        return {"ok": False, "error": "unknown_action", "state": "denied"}
    adapter = _detect_adapter()
    source = str(msg.get("source") or "unknown")
    request_id = str(msg.get("request_id") or "")
    peer = peer or {}
    _log(f"peer={peer} action={action} name={msg.get('name')} source={source} adapter={adapter} request_id={request_id}")
    with _LOCK:
        if action == "list":
            return {"ok": True, "adapter": adapter, "services": [_status_one(adapter, n) for n in LOGICAL_NAMES], "state": "ok"}
        if action == "status":
            name = msg.get("name") or "all"
            if name == "all":
                return handle({"action": "list", "source": source, "request_id": request_id}, peer=peer)
            resolved = resolve_service_name(name) if name != "all" else "all"
            if name != "all" and not resolved:
                return {"ok": False, "error": "not_allowlisted", "state": "denied"}
            return _status_one(adapter, resolved)
        name = msg.get("name")
        reason = str(msg.get("reason") or "").strip()
        if not name:
            return {"ok": False, "error": "name_required", "state": "failed"}
        if not reason:
            return {"ok": False, "error": "reason_required", "state": "failed"}
        if name == "all":
            if action != "restart":
                return {"ok": False, "error": "all_only_for_restart", "state": "denied"}
            results = []
            failures = 0
            for svc in restart_all_targets(source=source):
                r = _lifecycle_one(adapter, action, svc, source=source)
                results.append(r)
                st = r.get("state") or ""
                if st in ("failed", "denied", "timeout"):
                    failures += 1
                elif r.get("ok") is False and st not in ("unsupported", "skipped_source_guard"):
                    failures += 1
            if source in ("web", "browser"):
                results.append({"ok": True, "name": "web", "state": "skipped_source_guard",
                                "detail": "web transport cannot restart itself"})
            return {"ok": failures == 0, "action": action, "results": results,
                    "state": "partial_failure" if failures else "ok", "adapter": adapter}
        resolved = resolve_service_name(name)
        if not resolved:
            return {"ok": False, "error": "not_allowlisted", "state": "denied"}
        return _lifecycle_one(adapter, action, resolved, source=source)


def serve(sock_path: str) -> None:
    path = Path(sock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    try:
        import grp
        for gname in ("labvault-ops", "labvault"):
            try:
                os.chown(path, 0, grp.getgrnam(gname).gr_gid)
                break
            except KeyError:
                continue
    except (OSError, PermissionError):
        pass
    os.chmod(path, 0o660)
    server.listen(16)
    _log(f"listening on {path} adapter={_detect_adapter()}")
    while True:
        conn, _ = server.accept()
        with conn:
            peer = _peercred(conn)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            try:
                msg = json.loads(buf.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                resp = {"ok": False, "error": "bad_json", "state": "failed"}
            else:
                try:
                    resp = handle(msg, peer=peer)
                except Exception as exc:
                    _log(f"handler_error: {exc}")
                    resp = {"ok": False, "error": "internal_error", "state": "failed"}
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LabVault ops broker")
    ap.add_argument("--sock", default=SOCK)
    args = ap.parse_args(argv)
    try:
        serve(args.sock)
        return 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
