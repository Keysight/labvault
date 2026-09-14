"""Staff-only LabVault appliance CLI views (shared runner)."""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from connect.labvault_cli import commands as _commands  # noqa: F401 — register handlers
from connect.labvault_cli.runner import (
    CliContext,
    catalog_meta,
    invoke,
    prepare_confirm,
)
from connect.models import CliInvocation, CliJob


def _staff(user) -> bool:
    return bool(user.is_authenticated and user.is_staff)


def staff_required(view_func):
    @login_required
    def _wrapped(request, *args, **kwargs):
        if not _staff(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return _wrapped


def _ctx(request) -> CliContext:
    remote = (
        request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", "")
    )
    session_id = ""
    try:
        session_id = request.session.session_key or ""
    except Exception:  # noqa: BLE001
        session_id = ""
    return CliContext(
        user=request.user,
        source="web",
        remote_addr=remote,
        session_id=session_id,
    )


def _load_body(request) -> dict | None:
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


@staff_required
@ensure_csrf_cookie
def cli_page(request):
    return render(
        request,
        "connect/labvault_cli.html",
        {"commands": catalog_meta()},
    )


@staff_required
@require_GET
def cli_commands(request):
    return JsonResponse({"commands": catalog_meta()})


@staff_required
@require_POST
def cli_confirm(request):
    body = _load_body(request)
    if body is None:
        return JsonResponse({"error": "invalid_json", "state": "failed"}, status=400)
    result = prepare_confirm(_ctx(request), body.get("line") or body)
    return JsonResponse(result.as_dict(), status=result.status)


@staff_required
@require_POST
def cli_invoke(request):
    body = _load_body(request)
    if body is None:
        return JsonResponse({"error": "invalid_json", "state": "failed"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "invalid_body", "state": "failed"}, status=400)

    ctx = _ctx(request)
    # Attach source onto a synthetic request inside runner via ctx.source="web"
    line = body.get("line")
    if line is not None and str(line).strip():
        result = invoke(
            ctx,
            line=str(line),
            confirmation_nonce=body.get("confirmation_nonce")
            or body.get("confirmation_token"),
            idempotency_key=str(body.get("idempotency_key") or ""),
        )
    else:
        result = invoke(
            ctx,
            body=body,
            confirmation_nonce=body.get("confirmation_nonce")
            or body.get("confirmation_token"),
            idempotency_key=str(body.get("idempotency_key") or ""),
        )
    return JsonResponse(result.as_dict(), status=result.status)


@staff_required
@require_GET
def cli_job(request, job_id):
    try:
        job = CliJob.objects.get(pk=int(job_id))
    except (CliJob.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"error": "not_found"}, status=404)
    return JsonResponse(
        {
            "id": job.id,
            "command": job.command,
            "status": job.status,
            "result": job.result_redacted,
        }
    )


@staff_required
@require_GET
def cli_history(request):
    rows = CliInvocation.objects.all()[:100]
    return JsonResponse(
        {
            "history": [
                {
                    "id": r.id,
                    "command": r.command,
                    "outcome": r.outcome,
                    "source": r.source,
                    "created_at": r.created_at.isoformat(),
                    "args": r.args_redacted,
                    "reason": r.reason,
                    "target": r.target,
                    "result_state": r.result_state,
                }
                for r in rows
            ]
        }
    )
