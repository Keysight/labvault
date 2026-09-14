"""Canonical LabVault appliance service catalog (no Django dependency).

Logical names are identical on Compose, systemd, and airgap. Adapter fields map
those names onto docker compose services / systemd units.
"""
from __future__ import annotations

from typing import Any

# CLI / opsd logical names — same string on every deployment mode.
SERVICES: dict[str, dict[str, Any]] = {
    "web": {
        "systemd_unit": "labvault-web.service",
        "compose_service": "web",
        "status": True,
        "start": "ssh_only",
        "stop": False,
        "restart": "ssh_only",
        "restart_all_order": 50,
        "notes": "Web transport cannot stop/restart itself",
    },
    "heartbeat": {
        "systemd_unit": "labvault-heartbeat.service",
        "compose_service": "heartbeat",
        "status": True,
        "start": True,
        "stop": True,
        "restart": True,
        "restart_all_order": 10,
        "notes": "Verify heartbeat freshness after restart",
    },
    "collector": {
        "systemd_unit": "labvault-collector.service",
        "compose_service": "collector",
        "status": True,
        "start": True,
        "stop": True,
        "restart": True,
        "restart_all_order": 20,
        "notes": "Verify process and metric readiness",
    },
    "refresh": {
        "systemd_unit": "labvault-refresh.service",
        "compose_service": "refresh",
        "status": True,
        "start": True,
        "stop": True,
        "restart": True,
        "restart_all_order": 30,
        "notes": "Device/Keysight refresh worker",
    },
    "jobs": {
        "systemd_unit": "labvault-cli-worker.service",
        "compose_service": "jobs",
        "status": True,
        "start": True,
        "stop": True,
        "restart": True,
        "restart_all_order": 40,
        "notes": "CLI/background job worker",
    },
    "cli-ssh": {
        "systemd_unit": "labvault-cli-ssh.service",
        "compose_service": "cli-ssh",
        "status": True,
        "start": False,
        "stop": False,
        "restart": "web_only",
        "restart_all_order": None,
        "notes": "Avoid self-termination from SSH",
    },
    "opsd": {
        # Always the same unit name; Compose and bare-metal share labvault-opsd.service.
        # Legacy labvault-opsd-compose.service kept as a migration fallback for status.
        "systemd_unit": "labvault-opsd.service",
        "systemd_units": (
            "labvault-opsd.service",
            "labvault-opsd-compose.service",
        ),
        "compose_service": None,
        "host_only": True,
        "status": True,
        "start": False,
        "stop": False,
        "restart": False,
        "restart_all_order": None,
        "notes": "Broker cannot control itself",
    },
    "nginx": {
        "systemd_unit": "nginx.service",
        "compose_service": "nginx",
        "status": True,
        "start": False,
        "stop": False,
        "restart": "privileged",
        "restart_all_order": None,
        "notes": "Never included in restart-all",
    },
    "db": {
        "systemd_unit": None,
        "compose_service": "db",
        "status": True,
        "start": False,
        "stop": False,
        "restart": False,
        "restart_all_order": None,
        "notes": "Status-only safety boundary",
    },
    "metrics-db": {
        "systemd_unit": None,
        # Compose service name matches the logical name.
        "compose_service": "metrics-db",
        "status": True,
        "start": False,
        "stop": False,
        "restart": False,
        "restart_all_order": None,
        "notes": "Status-only safety boundary",
    },
}

# Accept common alternate spellings; always resolve to LOGICAL_NAMES.
NAME_ALIASES: dict[str, str] = {
    "metrics_db": "metrics-db",
    "metricsdb": "metrics-db",
    "cli_ssh": "cli-ssh",
    "clissh": "cli-ssh",
    "cli-worker": "jobs",
    "cli_worker": "jobs",
    "worker": "jobs",
    "opsd-compose": "opsd",
    "labvault-opsd": "opsd",
    "labvault-web": "web",
    "labvault-heartbeat": "heartbeat",
    "labvault-collector": "collector",
    "labvault-refresh": "refresh",
    "labvault-cli-ssh": "cli-ssh",
    "labvault-cli-worker": "jobs",
}

LOGICAL_NAMES = tuple(SERVICES.keys())
ACTIONS = frozenset({"list", "status", "start", "stop", "restart"})
RESULT_STATES = frozenset({
    "ok", "accepted", "already_running", "already_stopped", "unsupported",
    "denied", "timeout", "partial_failure", "failed", "skipped_source_guard",
})


def resolve_service_name(name: str) -> str | None:
    """Map user/alias input to a canonical logical service name."""
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in SERVICES:
        return key
    return NAME_ALIASES.get(key)


def restart_all_targets(*, source: str) -> list[str]:
    items = [
        (meta["restart_all_order"], name)
        for name, meta in SERVICES.items()
        if meta.get("restart_all_order") is not None
    ]
    items.sort()
    names = [n for _, n in items]
    if source in ("web", "browser"):
        names = [n for n in names if n != "web"]
    return names


def capability_allows(meta: dict[str, Any], action: str, *, source: str) -> tuple[bool, str]:
    flag = meta.get(action)
    if flag is True:
        return True, "ok"
    if flag is False or flag is None:
        return False, "denied"
    if flag == "ssh_only":
        return (True, "ok") if source == "ssh" else (False, "denied")
    if flag == "web_only":
        return (True, "ok") if source in ("web", "browser") else (False, "denied")
    if flag == "privileged":
        return False, "denied"
    return False, "denied"
