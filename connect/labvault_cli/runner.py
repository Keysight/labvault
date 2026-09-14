"""Shared LabVault CLI runner — web and SSH use the same path."""
from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.contrib.auth.models import AbstractBaseUser

from connect.labvault_cli.parser import ParseError, parse_invoke_body, parse_line
from connect.labvault_cli.redact import redact_payload
from connect.labvault_cli.registry import COMMANDS, get_command

_NONCES: dict[str, dict[str, Any]] = {}
_NONCE_LOCK = threading.Lock()
_NONCE_TTL_SEC = 120

PERM_CONTROL = "connect.control_labvault_services"


@dataclass
class CliContext:
    user: AbstractBaseUser
    source: str  # "web" | "ssh"
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    remote_addr: str = ""
    session_id: str = ""


@dataclass
class InvokeResult:
    ok: bool
    status: int
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.payload)
        out.setdefault("ok", self.ok)
        return out

    # SSH/web convenience
    @property
    def data(self) -> dict[str, Any]:
        return self.payload


def _staff_ok(user) -> bool:
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "is_staff", False)
    )


def can_control_services(user) -> bool:
    if not _staff_ok(user):
        return False
    if getattr(user, "is_superuser", False):
        return True
    try:
        return bool(user.has_perm(PERM_CONTROL))
    except Exception:  # noqa: BLE001
        return False


def issue_confirm_nonce(
    *,
    ctx: CliContext,
    command: str,
    args: dict[str, Any],
    action: str = "",
    target: str = "",
    reason: str = "",
) -> str:
    token = secrets.token_urlsafe(24)
    with _NONCE_LOCK:
        _NONCES[token] = {
            "user_id": getattr(ctx.user, "pk", None),
            "source": ctx.source,
            "session_id": ctx.session_id,
            "command": command,
            "args": dict(args),
            "action": action,
            "target": target,
            "reason": reason,
            "expires": time.time() + _NONCE_TTL_SEC,
        }
    return token


def consume_confirm_nonce(
    token: str,
    *,
    ctx: CliContext,
    command: str,
    args: dict[str, Any],
) -> tuple[bool, str]:
    if not token:
        return False, "confirmation_required"
    with _NONCE_LOCK:
        now = time.time()
        for k in [k for k, v in _NONCES.items() if v.get("expires", 0) < now]:
            _NONCES.pop(k, None)
        meta = _NONCES.pop(token, None)
    if not meta:
        return False, "invalid_or_expired_nonce"
    if meta.get("user_id") != getattr(ctx.user, "pk", None):
        return False, "nonce_user_mismatch"
    if meta.get("source") != ctx.source:
        return False, "nonce_source_mismatch"
    if ctx.session_id and meta.get("session_id") and meta["session_id"] != ctx.session_id:
        return False, "nonce_session_mismatch"
    if meta.get("command") != command:
        return False, "nonce_command_mismatch"
    if meta.get("reason") and args.get("reason") and meta["reason"] != args.get("reason"):
        return False, "nonce_reason_mismatch"
    return True, "ok"


def prepare_confirm(ctx: CliContext, line_or_body: str | dict) -> InvokeResult:
    """Parse a mutating command and return a one-time nonce (does not execute)."""
    try:
        if isinstance(line_or_body, str):
            command, args = parse_line(line_or_body)
        else:
            command, args = parse_invoke_body(line_or_body)
    except ParseError as exc:
        return InvokeResult(False, 400, {"error": exc.code, "detail": exc.detail, "state": "failed"})

    cmd = get_command(command)
    if not cmd:
        return InvokeResult(False, 404, {"error": "unknown_command", "state": "failed"})
    if not cmd.mutating or not cmd.requires_confirm:
        return InvokeResult(
            False,
            400,
            {"error": "confirmation_not_required", "command": command, "state": "failed"},
        )
    if not _staff_ok(ctx.user):
        return InvokeResult(False, 403, {"error": "staff_required", "state": "denied"})
    if cmd.tier in ("privileged", "service_control") and not can_control_services(ctx.user):
        return InvokeResult(False, 403, {"error": "permission_denied", "state": "denied"})

    reason = str(args.get("reason") or "").strip()
    if cmd.tier == "service_control" and not reason:
        return InvokeResult(
            False,
            400,
            {"error": "reason_required", "detail": 'provide reason="..."', "state": "failed"},
        )

    target = str(args.get("name") or args.get("target") or "")
    token = issue_confirm_nonce(
        ctx=ctx,
        command=command,
        args=args,
        action=command,
        target=target,
        reason=reason,
    )
    return InvokeResult(
        True,
        200,
        {
            "ok": True,
            "state": "accepted",
            "confirmation_required": True,
            "confirmation_nonce": token,
            "command": command,
            "args": redact_payload(args),
            "action": command,
            "target": target,
            "reason": reason,
            "expires_in_sec": _NONCE_TTL_SEC,
        },
    )


def _audit(
    *,
    ctx: CliContext,
    command: str,
    args: dict[str, Any],
    outcome: str,
    duration_ms: int,
    idem: str = "",
    result: Any = None,
) -> None:
    try:
        from connect.models import CliInvocation

        payload = result if isinstance(result, dict) else {"result": result}
        CliInvocation.objects.create(
            actor=ctx.user if getattr(ctx.user, "pk", None) else None,
            source="web" if ctx.source in ("web", "browser") else ctx.source,
            command=command,
            args_redacted=redact_payload(args),
            outcome=outcome,
            idempotency_key=idem[:64] if idem else "",
            correlation_id=ctx.request_id[:64],
            duration_ms=max(0, int(duration_ms)),
            reason=str(args.get("reason") or "")[:512],
            target=str(args.get("name") or args.get("target") or "")[:128],
            result_state=str((payload or {}).get("state") or outcome)[:32],
            result_redacted=redact_payload(payload) if isinstance(payload, dict) else {},
            remote_addr=(ctx.remote_addr or "")[:64],
        )
    except Exception:  # noqa: BLE001
        pass


def invoke(
    ctx: CliContext,
    *,
    line: str | None = None,
    body: dict[str, Any] | None = None,
    confirmation_nonce: str | None = None,
    idempotency_key: str = "",
) -> InvokeResult:
    """Execute one CLI command for an authenticated staff context."""
    start = time.time()
    try:
        if line is not None:
            command, args = parse_line(line)
        elif body is not None:
            command, args = parse_invoke_body(body)
            if confirmation_nonce is None:
                confirmation_nonce = body.get("confirmation_nonce") or body.get(
                    "confirmation_token"
                )
            if not idempotency_key:
                idempotency_key = str(body.get("idempotency_key") or "")
        else:
            return InvokeResult(False, 400, {"error": "missing_input", "state": "failed"})
    except ParseError as exc:
        return InvokeResult(False, 400, {"error": exc.code, "detail": exc.detail, "state": "failed"})

    if not _staff_ok(ctx.user):
        return InvokeResult(False, 403, {"error": "staff_required", "state": "denied"})

    cmd = get_command(command)
    if not cmd:
        return InvokeResult(
            False, 404, {"error": "unknown_command", "command": command, "state": "failed"}
        )

    if cmd.mutating and cmd.requires_confirm:
        if cmd.tier == "service_control":
            reason = str(args.get("reason") or "").strip()
            if not reason:
                return InvokeResult(
                    False,
                    400,
                    {
                        "error": "reason_required",
                        "detail": 'provide reason="..."',
                        "state": "failed",
                    },
                )
            if not can_control_services(ctx.user):
                return InvokeResult(False, 403, {"error": "permission_denied", "state": "denied"})
            ok_nonce, nonce_err = consume_confirm_nonce(
                str(confirmation_nonce or ""),
                ctx=ctx,
                command=command,
                args=args,
            )
            if not ok_nonce:
                return InvokeResult(False, 400, {"error": nonce_err, "state": "denied"})
        else:
            if cmd.tier == "privileged" and not (
                getattr(ctx.user, "is_superuser", False) or can_control_services(ctx.user)
            ):
                return InvokeResult(False, 403, {"error": "permission_denied", "state": "denied"})
            token = str(confirmation_nonce or "")
            if token and token != "confirm":
                ok_nonce, nonce_err = consume_confirm_nonce(
                    token, ctx=ctx, command=command, args=args
                )
                if not ok_nonce:
                    return InvokeResult(False, 400, {"error": nonce_err, "state": "denied"})
            elif token != "confirm":
                return InvokeResult(
                    False, 400, {"error": "confirmation_required", "state": "denied"}
                )

    class _Req:
        pass

    request = _Req()
    request.user = ctx.user
    request.cli_source = ctx.source
    request.cli_request_id = ctx.request_id
    request.META = {"REMOTE_ADDR": ctx.remote_addr}

    call_args = {k: v for k, v in args.items() if not k.startswith("_")}

    try:
        result = cmd.handler(request, **call_args)
    except TypeError as exc:
        _audit(
            ctx=ctx,
            command=command,
            args=args,
            outcome="error",
            duration_ms=int((time.time() - start) * 1000),
            idem=idempotency_key,
            result={"error": "invalid_args", "detail": str(exc)},
        )
        return InvokeResult(
            False,
            400,
            {"error": "invalid_args", "detail": str(exc), "state": "failed"},
        )
    except Exception:  # noqa: BLE001
        _audit(
            ctx=ctx,
            command=command,
            args=args,
            outcome="error",
            duration_ms=int((time.time() - start) * 1000),
            idem=idempotency_key,
            result={"error": "handler_failed"},
        )
        return InvokeResult(
            False,
            500,
            {"error": "handler_failed", "detail": "command failed", "state": "failed"},
        )

    payload = dict(result) if isinstance(result, dict) else {"result": result}
    payload.setdefault("state", "ok")
    redacted = redact_payload(payload)
    duration = int((time.time() - start) * 1000)
    bad = payload.get("state") in ("failed", "denied", "partial_failure")
    _audit(
        ctx=ctx,
        command=command,
        args=args,
        outcome="ok" if not bad else str(payload.get("state")),
        duration_ms=duration,
        idem=idempotency_key,
        result=redacted,
    )
    return InvokeResult(True, 200, {"ok": True, "result": redacted, "request_id": ctx.request_id})


def catalog_meta() -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in sorted(COMMANDS.values(), key=lambda x: x.name):
        if c.name in seen:
            continue
        seen.add(c.name)
        out.append(c.meta())
    return out


