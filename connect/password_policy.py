"""Password rules for shared/locked LabVault accounts (short team passwords allowed)."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.password_validation import get_password_validators, validate_password
from django.core.exceptions import ValidationError


def password_locked_usernames() -> frozenset[str]:
    """Lower-cased ``LABVAULT_PASSWORD_LOCKED_USERNAMES``."""
    raw = getattr(settings, 'LABVAULT_PASSWORD_LOCKED_USERNAMES', frozenset())
    return frozenset(x.strip().lower() for x in raw if x.strip())


def uses_relaxed_password_policy(user) -> bool:
    """Shared or locked accounts may use short legacy passwords (e.g. 5-char team default)."""
    if user is None:
        return False
    uname = (getattr(user, 'username', None) or '').strip().lower()
    return uname in password_locked_usernames()


def password_validators_for_user(user):
    """Validator instances for ``user``: none for relaxed accounts, else ``AUTH_PASSWORD_VALIDATORS``."""
    if uses_relaxed_password_policy(user):
        return []
    return get_password_validators(getattr(settings, 'AUTH_PASSWORD_VALIDATORS', []))


def validate_password_for_user(password: str, user) -> None:
    """Run Django password validators unless this user has a relaxed (shared) policy."""
    if not password:
        raise ValidationError('Password is required.', code='required')
    validate_password(
        password,
        user=user,
        password_validators=password_validators_for_user(user),
    )


def set_user_password(user, raw_password: str, *, validate: bool = True) -> None:
    """Optionally validate, then hash and save only the ``password`` column."""
    if validate:
        validate_password_for_user(raw_password, user)
    user.set_password(raw_password)
    user.save(update_fields=['password'])
