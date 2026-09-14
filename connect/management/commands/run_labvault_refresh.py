from django.core.management.base import BaseCommand
import time

class Command(BaseCommand):
    help = "Background device/Keysight refresh worker."

    def handle(self, *args, **options):
        self.stdout.write("labvault-refresh idle loop (no in-gunicorn polling)")
        while True:
            time.sleep(60)
