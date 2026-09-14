from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
import os
from pathlib import Path
import stat


DEMO_SECRETS = {
    "changeme",
    "password",
    "secret",
    "django-insecure",
    "labvault-demo",
    "admin",
}

DEMO_PASSWORDS = {
    "labvault!",
    "labvault-default-api-token",
}

PLACEHOLDER_DB = {
    "change_me",
    "changeme",
    "password",
    "example.com",
    "placeholder",
}


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class Command(BaseCommand):
    help = "Validate external customer configuration before the app binds ports."

    def handle(self, *args, **options):
        errors: list[str] = []
        secret = getattr(settings, "SECRET_KEY", "") or ""
        if not secret or len(secret) < 32:
            errors.append("DJANGO_SECRET_KEY missing or too short (<32)")
        low = secret.lower()
        if any(d in low for d in DEMO_SECRETS):
            errors.append("DJANGO_SECRET_KEY looks like a demo/placeholder value")
        if getattr(settings, "DEBUG", False) and os.environ.get("LABVAULT_ALLOW_DEBUG") != "1":
            errors.append("DEBUG must be false for customer deployments")
        hosts = getattr(settings, "ALLOWED_HOSTS", []) or []
        if not hosts:
            errors.append("DJANGO_ALLOWED_HOSTS is empty")
        if "*" in hosts:
            errors.append("DJANGO_ALLOWED_HOSTS must not contain *")
        origin = os.environ.get("LABVAULT_PUBLIC_ORIGIN", "")
        if origin and not (origin.startswith("https://") or origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1")):
            errors.append("LABVAULT_PUBLIC_ORIGIN must be https:// (or http://localhost / http://127.0.0.1)")
        cert = os.environ.get("LABVAULT_TLS_CERT", "")
        key = os.environ.get("LABVAULT_TLS_KEY", "")
        if origin.startswith("https://"):
            if not cert or not key:
                errors.append("LABVAULT_TLS_CERT/KEY required when using https origin")
            else:
                for label, path in (("cert", cert), ("key", key)):
                    p = Path(path)
                    if not p.is_file():
                        errors.append(f"TLS {label} missing: {path}")
                    elif label == "key":
                        mode = p.stat().st_mode & 0o777
                        if mode & 0o077:
                            errors.append(f"TLS key permissions too open: {oct(mode)}")
        for env_name in ("DATABASE_URL", "NP_TIMESERIES_DATABASE_URL"):
            val = (os.environ.get(env_name) or "").strip()
            if not val:
                # settings may still configure SQLite for tests
                continue
            low_val = val.lower()
            if any(p in low_val for p in PLACEHOLDER_DB):
                errors.append(f"{env_name} looks like a placeholder")
        if not settings.DATABASES.get("default"):
            errors.append("default database not configured")
        if "np_timeseries" not in settings.DATABASES:
            errors.append("np_timeseries database not configured")
        if not _env_true("LABVAULT_DEMO_DEFAULTS"):
            boot_pw = os.environ.get("LABVAULT_BOOTSTRAP_PASSWORD", "")
            if boot_pw in DEMO_PASSWORDS:
                errors.append("LABVAULT_BOOTSTRAP_PASSWORD is a published demo password")
            token = os.environ.get("LABVAULT_DEMO_API_TOKEN") or os.environ.get("LABVAULT_FLEET_TOKEN") or ""
            if token in DEMO_PASSWORDS:
                errors.append("fleet/demo API token is a published demo value")
        for env_path in (
            Path("/etc/labvault/labvault.env"),
            Path(os.environ.get("LABVAULT_ENV_FILE", "")),
        ):
            if not env_path or not env_path.is_file():
                continue
            mode = env_path.stat().st_mode & 0o777
            if mode & 0o077:
                errors.append(f"{env_path} permissions too open: {oct(mode)} (require 0600)")
        if errors:
            for e in errors:
                self.stderr.write(self.style.ERROR(e))
            raise CommandError(f"validate_external_config failed with {len(errors)} error(s)")
        self.stdout.write(self.style.SUCCESS("validate_external_config: OK"))
