"""SSH authentication helpers for the LabVault appliance CLI."""
from __future__ import annotations

import ipaddress
import os
from datetime import timedelta

from django.contrib.auth import authenticate, get_user_model
from django.utils import timezone

DEFAULT_ALLOW_CIDRS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
)


def allowed_cidrs() -> list[ipaddress._BaseNetwork]:
    raw = os.environ.get("LABVAULT_CLI_SSH_ALLOW_CIDRS", "").strip()
    items = [x.strip() for x in raw.split(",") if x.strip()] if raw else list(DEFAULT_ALLOW_CIDRS)
    out = []
    for item in items:
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return out


def addr_allowed(remote_addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return any(ip in net for net in allowed_cidrs())


def normalize_username(username: str) -> str:
    return (username or "").strip()[:150]


def _backoff_seconds(failure_count: int) -> int:
    # 1,2,4,8,16,32,60...
    return min(60, 2 ** max(0, failure_count - 1))


def throttle_status(username: str, remote_addr: str) -> tuple[bool, str, int]:
    """Return (allowed, reason, retry_after_sec)."""
    from connect.models import CliAuthThrottle

    username = normalize_username(username)
    remote_addr = (remote_addr or "")[:64]
    row = CliAuthThrottle.objects.filter(username=username, remote_addr=remote_addr).first()
    if not row:
        return True, "ok", 0
    now = timezone.now()
    if row.locked_until and row.locked_until > now:
        return False, "locked", max(1, int((row.locked_until - now).total_seconds()))
    return True, "ok", 0


def record_auth_failure(username: str, remote_addr: str) -> None:
    from connect.models import CliAuthThrottle

    username = normalize_username(username)
    remote_addr = (remote_addr or "")[:64]
    now = timezone.now()
    row, _ = CliAuthThrottle.objects.get_or_create(
        username=username,
        remote_addr=remote_addr,
        defaults={
            "failure_count": 0,
            "first_failure_at": now,
            "last_failure_at": now,
        },
    )
    row.failure_count = int(row.failure_count or 0) + 1
    if not row.first_failure_at:
        row.first_failure_at = now
    row.last_failure_at = now
    delay = _backoff_seconds(row.failure_count)
    # Lock after 8 failures for 5 minutes
    if row.failure_count >= 8:
        row.locked_until = now + timedelta(minutes=5)
    else:
        row.locked_until = now + timedelta(seconds=delay)
    row.save()


def record_auth_success(username: str, remote_addr: str) -> None:
    from connect.models import CliAuthThrottle

    username = normalize_username(username)
    remote_addr = (remote_addr or "")[:64]
    CliAuthThrottle.objects.filter(username=username, remote_addr=remote_addr).delete()


def authenticate_staff(username: str, password: str, remote_addr: str):
    """Authenticate active staff user or return (None, error_code)."""
    username = normalize_username(username)
    if not addr_allowed(remote_addr):
        return None, "cidr_denied"
    ok, reason, _retry = throttle_status(username, remote_addr)
    if not ok:
        return None, reason
    user = authenticate(username=username, password=password)
    if user is None:
        # Also try explicit model lookup + check_password for custom backends
        User = get_user_model()
        try:
            candidate = User.objects.get(username=username)
        except User.DoesNotExist:
            record_auth_failure(username, remote_addr)
            return None, "invalid_credentials"
        if not candidate.check_password(password):
            record_auth_failure(username, remote_addr)
            return None, "invalid_credentials"
        user = candidate
    if not getattr(user, "is_active", False) or not getattr(user, "is_staff", False):
        record_auth_failure(username, remote_addr)
        return None, "not_staff"
    record_auth_success(username, remote_addr)
    return user, "ok"

