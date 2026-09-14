"""Write a diagnostics JSON bundle (for incidents / support)."""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from connect.diagnostics import build_diagnostics_payload, build_diagnostics_tarball


class Command(BaseCommand):
    help = 'Export LabVault diagnostics JSON (DB health, env, recent errors, heartbeat).'

    def add_arguments(self, parser):
        parser.add_argument(
            '-o', '--output',
            default='',
            help='Output path (default: stdout)',
        )
        parser.add_argument('--no-logs', action='store_true', help='Omit recent log ring buffer')
        parser.add_argument('--log-limit', type=int, default=500)
        parser.add_argument(
            '--bundle',
            action='store_true',
            help='Write full multi-file tar.gz bundle instead of single JSON',
        )

    def handle(self, *args, **options):
        log_limit = max(50, options['log_limit'])
        out = options.get('output') or ''
        if options['bundle']:
            data = build_diagnostics_tarball(log_limit=log_limit)
            if out:
                path = Path(out)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                self.stdout.write(self.style.SUCCESS(f'Wrote bundle {path} ({len(data)} bytes)'))
            else:
                self.stdout.buffer.write(data)
            return

        payload = build_diagnostics_payload(
            include_logs=not options['no_logs'],
            log_limit=log_limit,
        )
        text = json.dumps(payload, indent=2, default=str)
        if out:
            path = Path(out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
            self.stdout.write(self.style.SUCCESS(f'Wrote {path}'))
        else:
            self.stdout.write(text)
        if not payload.get('ok'):
            self.stderr.write(self.style.WARNING('Diagnostics report: one or more checks failed'))
