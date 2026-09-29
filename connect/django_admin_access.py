"""Restrict Django ``/admin/`` to explicit break-glass usernames (default: godmode only)."""

from __future__ import annotations

from django.conf import settings
from django.contrib import admin


def django_admin_allowed_usernames() -> frozenset[str]:
    """Lower-cased ``LABVAULT_DJANGO_ADMIN_USERNAMES`` (default ``{'godmode'}``)."""
    raw = getattr(
        settings,
        'LABVAULT_DJANGO_ADMIN_USERNAMES',
        frozenset({'godmode'}),
    )
    return frozenset(x.strip().lower() for x in raw if x.strip())


def user_may_access_django_admin(user) -> bool:
    """True only for active staff users whose username is on the admin allowlist."""
    if not getattr(user, 'is_active', False):
        return False
    if not getattr(user, 'is_staff', False):
        return False
    uname = (getattr(user, 'username', None) or '').strip().lower()
    return uname in django_admin_allowed_usernames()


def patch_django_admin_site_access() -> None:
    """Limit the default AdminSite to LABVAULT_DJANGO_ADMIN_USERNAMES (idempotent)."""
    if getattr(admin.site, '_labvault_godmode_only_patched', False):
        return

    original = admin.AdminSite.has_permission

    def has_permission(self, request):
        if not original(self, request):
            return False
        return user_may_access_django_admin(request.user)

    admin.AdminSite.has_permission = has_permission  # type: ignore[method-assign]
    admin.site._labvault_godmode_only_patched = True  # type: ignore[attr-defined]
