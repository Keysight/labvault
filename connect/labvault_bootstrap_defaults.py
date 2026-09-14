"""First-boot login + fleet token.

Customer default is a random password written only to the 0600 credential file.
Published demo values (`admin` / `labvault!`) require explicit LABVAULT_DEMO_DEFAULTS=1.
"""
from __future__ import annotations

import os
import secrets

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "labvault!"
DEFAULT_FLEET_TOKEN_NAME = "demo-api"
DEFAULT_FLEET_TOKEN = "labvault-default-api-token"


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def bootstrap_username() -> str:
    return (os.environ.get("LABVAULT_BOOTSTRAP_USERNAME") or DEFAULT_ADMIN_USERNAME).strip() or DEFAULT_ADMIN_USERNAME


def bootstrap_password() -> str:
    explicit = (
        os.environ.get("LABVAULT_BOOTSTRAP_PASSWORD")
        or os.environ.get("LABVAULT_DEMO_ADMIN_PASSWORD")
        or ""
    ).strip()
    if explicit:
        return explicit
    if _env_true("LABVAULT_DEMO_DEFAULTS") and not _env_true("LABVAULT_BOOTSTRAP_RANDOM"):
        return DEFAULT_ADMIN_PASSWORD
    return secrets.token_urlsafe(20)


def fleet_token_name() -> str:
    return (os.environ.get("LABVAULT_FLEET_TOKEN_NAME") or DEFAULT_FLEET_TOKEN_NAME).strip() or DEFAULT_FLEET_TOKEN_NAME


def fleet_token_value() -> str:
    explicit = (
        os.environ.get("LABVAULT_FLEET_TOKEN")
        or os.environ.get("LABVAULT_DEMO_API_TOKEN")
        or ""
    ).strip()
    if explicit:
        return explicit
    if _env_true("LABVAULT_DEMO_DEFAULTS") and not _env_true("LABVAULT_BOOTSTRAP_RANDOM"):
        return DEFAULT_FLEET_TOKEN
    return secrets.token_urlsafe(32)
