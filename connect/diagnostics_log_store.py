"""Read aggregated service logs written by labvault-log-agent."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DEFAULT_SERVICES = ('web', 'collector', 'heartbeat', 'nginx', 'db')


def diagnostics_log_dir() -> Path | None:
    raw = os.environ.get('LABVAULT_DIAGNOSTICS_LOG_DIR', '').strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def read_host_logs(
    *,
    limit: int = 500,
    service: str | None = None,
    min_level: str = 'INFO',
) -> dict[str, Any]:
    """Return aggregated logs from JSONL files on shared volume."""
    log_dir = diagnostics_log_dir()
    out: dict[str, Any] = {
        'configured': log_dir is not None,
        'log_dir': str(log_dir) if log_dir else '',
        'services': {},
        'entries': [],
    }
    if log_dir is None:
        return out

    level_order = {'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50}
    threshold = level_order.get(min_level.upper(), 20)
    services = [service] if service else list(_DEFAULT_SERVICES)
    all_entries: list[dict[str, Any]] = []

    for svc in services:
        path = log_dir / f'{svc}.jsonl'
        svc_entries: list[dict[str, Any]] = []
        if not path.is_file():
            out['services'][svc] = {'path': str(path), 'exists': False, 'count': 0}
            continue
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            for line in lines[-max(limit * 2, 200):]:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                lvl = str(row.get('level', 'INFO')).upper()
                if level_order.get(lvl, 20) < threshold:
                    continue
                row.setdefault('service', svc)
                svc_entries.append(row)
            svc_entries = svc_entries[-limit:]
            all_entries.extend(svc_entries)
            out['services'][svc] = {
                'path': str(path),
                'exists': True,
                'count': len(svc_entries),
                'size_bytes': path.stat().st_size,
            }
        except OSError as exc:
            out['services'][svc] = {'path': str(path), 'exists': True, 'error': str(exc)}

    all_entries.sort(key=lambda r: str(r.get('ts', '')))
    out['entries'] = all_entries[-limit:]
    meta_path = log_dir / 'agent-meta.json'
    if meta_path.is_file():
        try:
            out['agent_meta'] = json.loads(meta_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass
    return out


def host_log_bundle_files(*, limit: int = 500) -> dict[str, bytes]:
    """Raw JSONL tails for tar.gz bundle."""
    log_dir = diagnostics_log_dir()
    files: dict[str, bytes] = {}
    if log_dir is None:
        return files
    for svc in _DEFAULT_SERVICES:
        path = log_dir / f'{svc}.jsonl'
        if path.is_file():
            text = '\n'.join(path.read_text(encoding='utf-8', errors='replace').splitlines()[-limit:])
            files[f'logs/{svc}.jsonl'] = text.encode('utf-8')
    summary = read_host_logs(limit=limit)
    files['logs/summary.json'] = json.dumps(summary, indent=2, default=str).encode('utf-8')
    return files
