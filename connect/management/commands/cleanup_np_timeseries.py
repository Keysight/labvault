"""
Delete aged rows from np_timeseries.sqlite3.

- LabMetricSample (raw 5m): 7 days
- LabMetricRollup (hourly): 31 days
- LabResourceEvent, PortUsageSample, NPResourceSample: 31 days

Cron (production example, daily at 03:00):

    0 3 * * * /opt/labvault/.venv/bin/python /opt/labvault/manage.py cleanup_np_timeseries

Set NP_TIMESERIES_DATABASE_URL if the time-series DB path differs from default.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from connect.models import (
    LabMetricRollup,
    LabMetricSample,
    LabResourceEvent,
    NPResourceSample,
    PortUsageSample,
)


class Command(BaseCommand):
    help = 'Prune np_timeseries rows (raw metrics 7d, rollups/events 31d).'

    def handle(self, *args, **options):
        now = timezone.now()
        raw_cutoff = now - timedelta(days=7)
        long_cutoff = now - timedelta(days=31)

        np_qs = NPResourceSample.objects.using('np_timeseries').filter(timestamp__lt=long_cutoff)
        np_deleted, _ = np_qs.delete()

        port_qs = PortUsageSample.objects.using('np_timeseries').filter(started_at__lt=long_cutoff)
        port_deleted, _ = port_qs.delete()

        metric_qs = LabMetricSample.objects.using('np_timeseries').filter(sampled_at__lt=raw_cutoff)
        metric_deleted, _ = metric_qs.delete()

        rollup_qs = LabMetricRollup.objects.using('np_timeseries').filter(bucket_start__lt=long_cutoff)
        rollup_deleted, _ = rollup_qs.delete()

        event_qs = LabResourceEvent.objects.using('np_timeseries').filter(started_at__lt=long_cutoff)
        event_deleted, _ = event_qs.delete()

        total = np_deleted + port_deleted + metric_deleted + rollup_deleted + event_deleted
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {total} row(s) '
            f'(NPResourceSample={np_deleted}, PortUsageSample={port_deleted}, '
            f'LabMetricSample raw<{raw_cutoff.date()}={metric_deleted}, '
            f'LabMetricRollup={rollup_deleted}, LabResourceEvent={event_deleted})'
        ))
