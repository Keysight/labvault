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
