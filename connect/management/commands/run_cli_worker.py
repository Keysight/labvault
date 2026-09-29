"""Drain queued ``CliJob`` rows.

Long-running worker (opsd service ``jobs``, systemd ``labvault-jobs`` / compose
``jobs``). Every 2 seconds it marks the oldest ``queued`` job ``succeeded`` with
``result_redacted={"ok": True}``. It does not call the CLI registry, so queued jobs
are not actually executed. Nothing in this tree enqueues ``CliJob`` rows yet.
See ``docs/development/subsystems/cli.md`` and ``operations.md``.
"""
from django.core.management.base import BaseCommand
import time
from connect.models import CliJob

class Command(BaseCommand):
    help = "Process queued LabVault CLI jobs."

    def handle(self, *args, **options):
        self.stdout.write("cli worker started")
        while True:
            job = CliJob.objects.filter(status="queued").order_by("id").first()
            if job:
                job.status = "running"
                job.save(update_fields=["status", "updated_at"])
                job.status = "succeeded"
                job.result_redacted = {"ok": True}
                job.save(update_fields=["status", "result_redacted", "updated_at"])
                self.stdout.write(f"job {job.id} done")
            time.sleep(2)
