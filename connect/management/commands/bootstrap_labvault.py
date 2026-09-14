"""First-boot: restore dataset, enable Lab Pulse workers, seed fleet API token."""
from __future__ import annotations

import os
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from connect.labvault_bootstrap_defaults import (
    bootstrap_password,
    bootstrap_username,
    fleet_token_name,
    fleet_token_value,
)


class Command(BaseCommand):
    help = "Enable live pulse, seed fleet Bearer token, optionally import a LabVault dataset."

    def add_arguments(self, parser):
        parser.add_argument("--restore", default="", help="Path to labvault-full-export JSON")
        parser.add_argument(
            "--live",
            action="store_true",
            help="Set collector_mode + heartbeat_mode to live (Lab Pulse on).",
        )
        parser.add_argument("--fleet-token", default="", help="Bearer token value (default: labvault-default-api-token)")
        parser.add_argument("--fleet-token-name", default="")
        parser.add_argument(
            "--token-file",
            default="",
            help="Write token to this path (0600). Default: /tmp/labvault-fleet-token",
        )
        parser.add_argument(
            "--include-logs",
            action="store_true",
            help="Import audit/changelog rows (slow). Default skips them.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        from connect.models import APIToken
        from connect.runtime_settings import set_setting

        live = options["live"] or os.environ.get("LABVAULT_WORKER_MODE", "").strip().lower() == "live"
        restore = (options["restore"] or os.environ.get("LABVAULT_RESTORE_DATASET") or "").strip()
        if restore:
            live = True

        if live:
            set_setting("collector_mode", "live")
            set_setting("heartbeat_mode", "live")
            self.stdout.write("pulse=live (collector_mode=live heartbeat_mode=live)")
        else:
            self.stdout.write("pulse=idle")

        username = bootstrap_username()
        password = bootstrap_password()
        admin = (
            User.objects.filter(username=username).first()
            or User.objects.filter(is_superuser=True).order_by("pk").first()
            or User.objects.filter(is_staff=True).order_by("pk").first()
        )
        created_admin = False
        if admin is None:
            admin = User.objects.create_superuser(username, f"{username}@localhost", password)
            created_admin = True
            self.stdout.write(f"admin_created username={username}")
        else:
            # Re-runs must not reset an existing password (install idempotency).
            if not admin.is_superuser or not admin.is_staff:
                admin.is_superuser = True
                admin.is_staff = True
                admin.save(update_fields=["is_superuser", "is_staff"])
            self.stdout.write(f"admin_exists username={admin.get_username()}")
        # Grant appliance service-control permission to bootstrap admin
        try:
            from django.contrib.auth.models import Permission
            perm = Permission.objects.get(codename="control_labvault_services", content_type__app_label="connect")
            admin.user_permissions.add(perm)
        except Exception:
            pass

        explicit_token = (options["fleet_token"] or "").strip()
        name = (options["fleet_token_name"] or "").strip() or fleet_token_name()
        row = APIToken.objects.filter(user=admin, name=name).first()
        if row is None:
            token_val = explicit_token or fleet_token_value()
            clash = APIToken.objects.filter(token=token_val)
            clash.delete()
            row = APIToken.objects.create(user=admin, name=name, token=token_val, enabled=True)
            self.stdout.write(f"fleet_token_created name={name}")
        elif explicit_token:
            token_val = explicit_token
            clash = APIToken.objects.filter(token=token_val).exclude(pk=row.pk)
            clash.delete()
            row.token = token_val
            row.enabled = True
            row.save(update_fields=["token", "enabled"])
            self.stdout.write(f"fleet_token_set name={name}")
        else:
            token_val = row.token
            if not row.enabled:
                row.enabled = True
                row.save(update_fields=["enabled"])
            self.stdout.write(f"fleet_token_unchanged name={name}")

        token_file = Path(options["token_file"] or "/tmp/labvault-fleet-token")
        token_file.write_text(f"name={name}\ntoken={token_val}\n", encoding="utf-8")
        try:
            os.chmod(token_file, 0o600)
        except OSError:
            pass
        self.stdout.write(f"fleet_token_file={token_file}")

        cred = Path("/tmp/bootstrap-credentials")
        if created_admin:
            cred.write_text(
                f"username={admin.get_username()}\npassword={password}\nchange_on_first_login=recommended\n",
                encoding="utf-8",
            )
        else:
            cred.write_text(
                f"username={admin.get_username()}\npassword_unchanged=1\n"
                "note=Existing account; password was not reset\n",
                encoding="utf-8",
            )
        try:
            os.chmod(cred, 0o600)
        except OSError:
            pass

        if restore:
            path = Path(restore)
            if not path.is_file():
                raise CommandError(f"restore file not found: {path}")
            from connect.labvault_dataset import import_from_file

            stats = import_from_file(
                str(path),
                import_capex=False,
                skip_logs=not options["include_logs"],
            )
            self.stdout.write(self.style.SUCCESS(f"restore_ok {stats}"))
        else:
            self.stdout.write("restore=skipped")
