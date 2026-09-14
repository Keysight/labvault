"""LabVault appliance CLI command handlers."""
from __future__ import annotations

from connect.labvault_cli.registry import COMMANDS, command
from connect.labvault_cli.service_catalog import SERVICES, restart_all_targets


@command("help", "List available LabVault CLI commands")
def help_cmd(request, cmd: str = ""):
    if cmd:
        c = COMMANDS.get(cmd)
        return {"command": cmd, "help": c.help if c else "unknown", "state": "ok"}
    # Prefer canonical names (skip alias-only entries that start with "Alias for")
    names = sorted(
        {
            n
            for n, c in COMMANDS.items()
            if not (c.help or "").startswith("Alias for")
        }
    )
    return {"commands": names, "state": "ok"}


@command("whoami", "Show current operator identity")
def whoami(request):
    u = request.user
    from connect.labvault_cli.runner import can_control_services

    return {
        "username": u.get_username(),
        "is_staff": bool(u.is_staff),
        "is_superuser": bool(u.is_superuser),
        "can_control_services": can_control_services(u),
        "state": "ok",
    }


@command(
    "show devices",
    "List devices",
    aliases=("device list",),
)
def show_devices(request, limit: int = 50):
    from connect.models import Device

    qs = Device.objects.all()[: int(limit)]
    return {
        "devices": [
            {
                "id": d.id,
                "name": d.hostname or d.ip_address,
                "ip": d.ip_address,
                "vendor": d.vendor_type,
            }
            for d in qs
        ],
        "state": "ok",
    }


@command(
    "show chassis",
    "List Keysight chassis",
    aliases=("chassis list",),
)
def show_chassis(request, limit: int = 50):
    from connect.models import KeysightChassis

    qs = KeysightChassis.objects.all()[: int(limit)]
    return {
        "chassis": [
            {
                "id": c.id,
                "name": getattr(c, "hostname", None) or getattr(c, "name", "") or "",
                "ip": getattr(c, "ip_address", "") or "",
            }
            for c in qs
        ],
        "state": "ok",
    }


@command(
    "show topologies",
    "List lab topologies",
    aliases=("topo list",),
)
def show_topologies(request, limit: int = 50):
    from connect.models import LabTopology

    qs = LabTopology.objects.all()[: int(limit)]
    return {
        "topologies": [{"id": t.id, "name": getattr(t, "name", "")} for t in qs],
        "state": "ok",
    }


@command(
    "show fleet",
    "Fleet heartbeat summary",
    aliases=("fleet health",),
)
def show_fleet(request):
    from connect.fleet_heartbeat import fleet_heartbeat_payload, heartbeat_mode

    payload = fleet_heartbeat_payload()
    return {
        "status": heartbeat_mode(),
        "counts": payload.get("counts") or {},
        "mode": payload.get("mode"),
        "updated_at": payload.get("updated_at"),
        "state": "ok",
    }


@command(
    "show settings",
    "List runtime settings",
    aliases=("settings list",),
)
def show_settings(request):
    from connect.runtime_settings import SCHEMA, get_setting

    return {"settings": {k: get_setting(k) for k in SCHEMA}, "state": "ok"}


@command(
    "settings set",
    "Set a runtime setting",
    mutating=True,
    requires_confirm=True,
    tier="privileged",
)
def settings_set(request, key: str = "", value: str = ""):
    from connect.runtime_settings import set_setting

    row = set_setting(key, value, user=request.user)
    return {"key": row.key, "value": value, "version": row.version, "state": "ok"}


@command(
    "show insights",
    "Topology insights / collector status",
    aliases=("insights status",),
)
def show_insights(request):
    from connect.runtime_settings import get_setting

    return {
        "collector_mode": get_setting("collector_mode"),
        "heartbeat_mode": get_setting("heartbeat_mode"),
        "state": "ok",
    }


@command(
    "show services",
    "Show LabVault service components",
    aliases=("service list", "service status"),
)
def show_services(request, name: str = "all"):
    from connect.labvault_cli.ops_client import list_services, status_service

    if name and name != "all":
        return status_service(name)
    return {"services": list_services(), "state": "ok"}


def _lifecycle(request, action: str, name: str = "", reason: str = ""):
    from connect.labvault_cli.ops_client import lifecycle

    source = getattr(request, "cli_source", "web") or "web"
    request_id = getattr(request, "cli_request_id", "") or ""
    name = (name or "").strip()
    reason = (reason or "").strip()
    if not name:
        return {"ok": False, "error": "name_required", "state": "failed"}
    if not reason:
        return {"ok": False, "error": "reason_required", "state": "failed"}

    if name == "all":
        if action != "restart":
            return {"ok": False, "error": "all_only_for_restart", "state": "denied"}
        targets = restart_all_targets(source=source)
        results = []
        failures = 0
        for svc in targets:
            if svc not in SERVICES:
                continue
            r = lifecycle(
                action,
                svc,
                source=source,
                reason=reason,
                request_id=request_id,
            )
            results.append({"name": svc, **r})
            st = r.get("state") or ""
            if st in ("failed", "denied", "timeout") or (
                r.get("ok") is False and st not in ("unsupported", "skipped_source_guard", "already_running", "already_stopped")
            ):
                if st not in ("unsupported", "skipped_source_guard"):
                    failures += 1
        # Mark web as skipped when invoked from web
        if source in ("web", "browser"):
            results.append(
                {
                    "name": "web",
                    "ok": True,
                    "state": "skipped_source_guard",
                    "detail": "web transport cannot restart itself",
                }
            )
        state = "partial_failure" if failures else "ok"
        return {"ok": failures == 0, "action": action, "results": results, "state": state}

    if name not in SERVICES:
        return {"ok": False, "error": "unknown_service", "name": name, "state": "denied"}

    return lifecycle(
        action,
        name,
        source=source,
        reason=reason,
        request_id=request_id,
    )


@command(
    "service start",
    "Start a LabVault service",
    mutating=True,
    requires_confirm=True,
    tier="service_control",
)
def service_start(request, name: str = "", reason: str = ""):
    return _lifecycle(request, "start", name=name, reason=reason)


@command(
    "service stop",
    "Stop a LabVault service",
    mutating=True,
    requires_confirm=True,
    tier="service_control",
)
def service_stop(request, name: str = "", reason: str = ""):
    return _lifecycle(request, "stop", name=name, reason=reason)


@command(
    "service restart",
    "Restart a LabVault service (or all)",
    mutating=True,
    requires_confirm=True,
    tier="service_control",
)
def service_restart(request, name: str = "", reason: str = ""):
    return _lifecycle(request, "restart", name=name, reason=reason)


@command(
    "device show",
    "Show one device by id or IP",
    aliases=("show device",),
    fields=("id_or_ip",),
    guided=True,
)
def device_show(request, id_or_ip: str = ""):
    from connect.models import Device

    qs = Device.objects.all()
    row = None
    if str(id_or_ip).isdigit():
        row = qs.filter(pk=int(id_or_ip)).first()
    if row is None:
        row = qs.filter(ip_address=id_or_ip).first() or qs.filter(hostname=id_or_ip).first()
    if row is None:
        return {"ok": False, "error": "not_found", "state": "failed"}
    return {
        "device": {
            "id": row.id,
            "hostname": row.hostname,
            "ip": row.ip_address,
            "vendor": row.vendor_type,
            "status": row.status,
        },
        "state": "ok",
    }


@command(
    "device add",
    "Add a device (guided)",
    mutating=True,
    requires_confirm=True,
    tier="privileged",
    aliases=("add device",),
    fields=("ip_address", "vendor_type", "username", "password", "hostname"),
    guided=True,
)
def device_add(
    request,
    ip_address: str = "",
    vendor_type: str = "arista",
    username: str = "admin",
    password: str = "",
    hostname: str = "",
):
    from connect.models import Device

    if not ip_address:
        return {"ok": False, "error": "ip_address_required", "state": "failed"}
    row, created = Device.objects.get_or_create(
        ip_address=ip_address,
        defaults={
            "vendor_type": vendor_type or "arista",
            "username": username or "admin",
            "password": password or "",
            "hostname": hostname or ip_address,
            "status": "unknown",
        },
    )
    return {"id": row.id, "created": created, "ip": row.ip_address, "state": "ok"}


@command(
    "chassis show",
    "Show one chassis by id or IP",
    aliases=("show chassis detail",),
    fields=("id_or_ip",),
    guided=True,
)
def chassis_show(request, id_or_ip: str = ""):
    from connect.models import KeysightChassis

    qs = KeysightChassis.objects.all()
    row = None
    if str(id_or_ip).isdigit():
        row = qs.filter(pk=int(id_or_ip)).first()
    if row is None:
        row = qs.filter(ip_address=id_or_ip).first()
    if row is None:
        return {"ok": False, "error": "not_found", "state": "failed"}
    return {
        "chassis": {
            "id": row.id,
            "hostname": getattr(row, "hostname", ""),
            "ip": row.ip_address,
            "status": getattr(row, "status", ""),
        },
        "state": "ok",
    }


@command(
    "reserve list",
    "List Keysight reservations",
    aliases=("show reservations",),
)
def reserve_list(request, limit: int = 50):
    from connect.models import KeysightReservation

    qs = KeysightReservation.objects.all()[: int(limit)]
    return {
        "reservations": [
            {
                "id": r.id,
                "name": getattr(r, "name", "") or getattr(r, "title", ""),
                "status": getattr(r, "status", ""),
            }
            for r in qs
        ],
        "state": "ok",
    }


@command(
    "reserve create",
    "Create a Keysight reservation (guided)",
    mutating=True,
    requires_confirm=True,
    tier="privileged",
    fields=("name", "notes"),
    guided=True,
)
def reserve_create(request, name: str = "", notes: str = ""):
    from datetime import timedelta

    from django.utils import timezone

    from connect.models import KeysightReservation

    if not name:
        return {"ok": False, "error": "name_required", "state": "failed"}
    now = timezone.now()
    row = KeysightReservation.objects.create(
        title=name,
        description=notes or "",
        user=request.user,
        start_time=now,
        end_time=now + timedelta(hours=1),
        status="upcoming",
    )
    return {"id": row.id, "name": name, "state": "ok"}


@command("audit list", "Recent audit log entries", aliases=("show audit",))
def audit_list(request, limit: int = 25):
    from connect.models import AuditLog

    qs = AuditLog.objects.all().order_by("-id")[: int(limit)]
    return {
        "audit": [
            {
                "id": a.id,
                "action": getattr(a, "action", ""),
                "user": str(getattr(a, "user", "") or ""),
            }
            for a in qs
        ],
        "state": "ok",
    }


@command(
    "lldp refresh",
    "Refresh LLDP for one device IP (allowlisted)",
    mutating=True,
    requires_confirm=True,
    tier="privileged",
    fields=("ip",),
    guided=True,
)
def lldp_refresh(request, ip: str = ""):
    from connect.models import Device
    from connect.drivers import get_driver

    if not ip:
        return {"ok": False, "error": "ip_required", "state": "failed"}
    device = Device.objects.filter(ip_address=ip).first()
    if device is None:
        return {"ok": False, "error": "not_found", "state": "failed"}
    drv = get_driver(device)
    res = drv.get_lldp_neighbors_detail()
    n = len(res.data) if getattr(res, "success", False) and res.data else 0
    return {"ip": ip, "neighbors": n, "ok": bool(getattr(res, "success", False)), "state": "ok"}


@command("settings get", "Get one runtime setting", fields=("key",), guided=True)
def settings_get(request, key: str = ""):
    from connect.runtime_settings import get_setting

    if not key:
        return {"ok": False, "error": "key_required", "state": "failed"}
    return {"key": key, "value": get_setting(key), "state": "ok"}


@command("smoke", "Empty-lab readiness smoke")
def smoke(request):
    from django.test import Client

    c = Client()
    live = c.get("/health/live")
    ready = c.get("/health/ready")
    return {
        "live": live.status_code,
        "ready": ready.status_code,
        "state": "ok" if live.status_code == 200 else "failed",
    }


@command(
    "topo import",
    "Import a retained topology JSON path (guided)",
    mutating=True,
    requires_confirm=True,
    tier="privileged",
    fields=("path",),
    guided=True,
)
def topo_import(request, path: str = ""):
    if not path:
        return {"ok": False, "error": "path_required", "state": "failed"}
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return {"ok": False, "error": "file_not_found", "state": "failed"}
    return {"path": str(p), "state": "ok", "note": "file present; use UI import to apply"}


@command("diag cheap", "Cheap empty-lab-safe diagnostics snapshot")
def diag_cheap(request):
    from connect.models import Device, KeysightChassis, LabTopology
    from connect.runtime_settings import get_setting

    return {
        "status": "ok",
        "devices": Device.objects.count(),
        "chassis": KeysightChassis.objects.count(),
        "topologies": LabTopology.objects.count(),
        "collector_mode": get_setting("collector_mode"),
        "heartbeat_mode": get_setting("heartbeat_mode"),
        "state": "ok",
    }


@command("commands", "Alias for help")
def commands_cmd(request):
    return help_cmd(request)


@command("context", "Show SKU context")
def context_cmd(request):
    return {
        "product": "LabVault",
        "sku": "customer",
        "omitted": ["capex", "hyperview", "laas", "ai_nexus", "snappi", "demo_stage"],
        "state": "ok",
    }


@command("alerts list", "List recent alerts", aliases=("show alerts",))
def alerts_list(request, limit: int = 25):
    from connect.models import Alert

    qs = Alert.objects.all().order_by("-id")[: int(limit)]
    return {
        "alerts": [
            {
                "id": a.id,
                "severity": a.severity,
                "type": a.alert_type,
                "message": a.message[:200],
            }
            for a in qs
        ],
        "state": "ok",
    }


@command("token list", "List API token names (values redacted)")
def token_list(request):
    from connect.models import APIToken

    qs = APIToken.objects.filter(user=request.user)
    return {
        "tokens": [
            {
                "id": t.id,
                "name": t.name,
                "enabled": t.enabled,
                "prefix": (t.token or "")[:4] + "…",
            }
            for t in qs
        ],
        "state": "ok",
    }


@command("history", "Recent CLI invocations")
def history_cmd(request, limit: int = 20):
    from connect.models import CliInvocation

    qs = CliInvocation.objects.all().order_by("-id")[: int(limit)]
    return {
        "history": [
            {
                "id": h.id,
                "command": h.command,
                "outcome": h.outcome,
                "state": h.result_state,
            }
            for h in qs
        ],
        "state": "ok",
    }


@command(
    "device command",
    "Allowlisted device probe (not a free-form shell)",
    mutating=True,
    requires_confirm=True,
    tier="privileged",
    fields=("id_or_ip", "verb"),
    guided=True,
)
def device_command(request, id_or_ip: str = "", verb: str = "status"):
    from connect.drivers import get_driver
    from connect.models import Device

    allowed = {"status", "probe", "lldp"}
    verb = (verb or "status").strip().lower()
    if verb not in allowed:
        return {"ok": False, "error": "verb_not_allowlisted", "allowed": sorted(allowed), "state": "denied"}
    qs = Device.objects.all()
    device = None
    if str(id_or_ip).isdigit():
        device = qs.filter(pk=int(id_or_ip)).first()
    if device is None:
        device = qs.filter(ip_address=id_or_ip).first()
    if device is None:
        return {"ok": False, "error": "not_found", "state": "failed"}
    drv = get_driver(device)
    if verb == "lldp":
        res = drv.get_lldp_neighbors_detail()
    elif verb == "probe" and hasattr(drv, "get_system_info"):
        res = drv.get_system_info()
    else:
        res = drv.get_lldp_neighbors()
    return {
        "id": device.id,
        "ip": device.ip_address,
        "verb": verb,
        "ok": bool(getattr(res, "success", False)),
        "state": "ok" if getattr(res, "success", False) else "failed",
    }


@command(
    "reserve cancel",
    "Cancel a Keysight reservation",
    mutating=True,
    requires_confirm=True,
    tier="privileged",
    fields=("id",),
    guided=True,
)
def reserve_cancel(request, id: str = ""):
    from connect.models import KeysightReservation

    if not str(id).isdigit():
        return {"ok": False, "error": "id_required", "state": "failed"}
    row = KeysightReservation.objects.filter(pk=int(id)).first()
    if row is None:
        return {"ok": False, "error": "not_found", "state": "failed"}
    row.status = "cancelled"
    row.save(update_fields=["status"])
    return {"id": row.id, "status": row.status, "state": "ok"}

