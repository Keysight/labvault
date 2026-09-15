"""Background Keysight card/port refresh (gunicorn does not poll in-process)."""
from __future__ import annotations

import os
import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Probe Keysight chassis and refresh card/port cache (not an idle loop)."

    def handle(self, *args, **options):
        try:
            interval = max(30, int(os.environ.get("LABVAULT_KS_REFRESH_INTERVAL", "120")))
        except (TypeError, ValueError):
            interval = 120
        from connect.keysight_views import ks_refresh_once

        self.stdout.write(f"labvault-refresh starting interval={interval}s")
        while True:
            try:
                n = ks_refresh_once()
                self.stdout.write(f"refresh ok chassis={n}")
            except Exception as exc:
                self.stderr.write(f"refresh error: {exc}")
            time.sleep(interval)
