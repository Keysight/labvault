"""Slack integration for Keysight lab status (slash commands + HW error webhooks).

Configuration (all optional; Slack stays inert when unset):

* ``SLACK_SIGNING_SECRET`` — required for the ``/api/slack/keysight/`` slash
  command; requests are rejected (403) when it is missing or the v0 HMAC /
  5-minute timestamp window fails.
* ``KEYSIGHT_SLACK_WEBHOOK_URL`` — comma-separated incoming-webhook URLs.
  Enabled ``WebhookEndpoint`` rows with ``webhook_type='slack'`` are added.
* ``LABVAULT_PUBLIC_HOSTNAME`` — when set, messages link to
  ``https://<host>/keysight/chassis/<id>/``; otherwise relative paths are used.

Outbound posts are synchronous ``requests.post`` calls (8 s timeout each) made
from the request thread that changed a flag.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from urllib.parse import urljoin

import requests

from .keysight_hw_errors import collect_hw_error_report
from .models import KeysightChassis, WebhookEndpoint

logger = logging.getLogger(__name__)

_MAX_SLACK_LINES = 25


def slack_signing_secret() -> str:
    """``SLACK_SIGNING_SECRET`` from the environment ('' when unset)."""
    return (os.environ.get('SLACK_SIGNING_SECRET') or '').strip()


def slack_webhook_urls() -> list[str]:
    """Env webhook URLs plus enabled Slack ``WebhookEndpoint`` rows, de-duplicated."""
    urls: list[str] = []
    raw = (os.environ.get('KEYSIGHT_SLACK_WEBHOOK_URL') or '').strip()
    if raw:
        urls.extend(u.strip() for u in raw.split(',') if u.strip())
    for ep in WebhookEndpoint.objects.filter(enabled=True, webhook_type='slack'):
        if ep.url and ep.url not in urls:
            urls.append(ep.url)
    return urls


def verify_slack_signature(request) -> bool:
    """Validate Slack's ``X-Slack-Signature`` (v0 HMAC-SHA256) and request timestamp."""
    secret = slack_signing_secret()
    if not secret:
        logger.warning('Slack request rejected: SLACK_SIGNING_SECRET not configured')
        return False
    ts = request.headers.get('X-Slack-Request-Timestamp', '')
    sig = request.headers.get('X-Slack-Signature', '')
    if not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 60 * 5:
            return False
    except ValueError:
        return False
    body = request.body.decode('utf-8') if request.body else ''
    base = f'v0:{ts}:{body}'
    expected = 'v0=' + hmac.new(
        secret.encode(), base.encode(), hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


def _labvault_base_url() -> str:
    host = (os.environ.get('LABVAULT_PUBLIC_HOSTNAME') or '').strip()
    if host:
        return f'https://{host}'
    return ''


def _chassis_detail_url(chassis_id: int | None) -> str:
    if not chassis_id:
        return ''
    base = _labvault_base_url()
    if not base:
        return f'/keysight/chassis/{chassis_id}/'
    return urljoin(base + '/', f'keysight/chassis/{chassis_id}/')


def format_hw_errors_text(rows: list[dict] | None = None) -> str:
    """Slack mrkdwn list of flagged units (capped at ``_MAX_SLACK_LINES``)."""
    rows = rows if rows is not None else collect_hw_error_report()
    if not rows:
        return 'No reported hardware issues in LabVault.'
    lines = [f'*LabVault hardware issues* ({len(rows)} unit{"s" if len(rows) != 1 else ""})']
    for row in rows[:_MAX_SLACK_LINES]:
        label = row['chassis_label']
        if row.get('node_name'):
            label = f'{label} / `{row["node_name"]}`'
        note = (row.get('notes') or '').strip()
        suffix = f' — {note}' if note else ''
        url = _chassis_detail_url(row.get('chassis_id'))
        if url:
            lines.append(f'• <{url}|{label}>{suffix}')
        else:
            lines.append(f'• {label}{suffix}')
    if len(rows) > _MAX_SLACK_LINES:
        lines.append(f'_…and {len(rows) - _MAX_SLACK_LINES} more_')
    dash = _labvault_base_url()
    if dash:
        lines.append(f'<{urljoin(dash + "/", "keysight/")}|Open Keysight dashboard>')
    return '\n'.join(lines)


def handle_slack_command(text: str) -> dict:
    """Build slash-command JSON response for Slack.

    Commands: ``help`` (default), ``hw-errors`` / ``bad`` / ``hw`` / ``hardware``,
    ``count`` / ``hw-count``. Unknown commands get an ephemeral hint.
    """
    cmd = (text or '').strip().lower()
    if cmd in ('', 'help'):
        return {
            'response_type': 'ephemeral',
            'text': (
                '*LabVault bot commands*\n'
                '• `hw-errors` — list flagged chassis / CNs with known hardware issues\n'
                '• `bad` — alias for hw-errors\n'
                '• `count` — number of flagged units only'
            ),
        }
    if cmd in ('hw-errors', 'bad', 'hw', 'hardware'):
        rows = collect_hw_error_report()
        return {
            'response_type': 'in_channel',
            'text': format_hw_errors_text(rows),
        }
    if cmd in ('count', 'hw-count'):
        n = len(collect_hw_error_report())
        return {
            'response_type': 'in_channel',
            'text': f'{n} unit{"s" if n != 1 else ""} flagged with reported hardware issues.',
        }
    return {
        'response_type': 'ephemeral',
        'text': f'Unknown command `{cmd}`. Try `help`.',
    }


def post_slack_message(text: str, *, username: str = 'LabVault') -> None:
    """POST ``text`` to every configured webhook; failures are logged, not raised."""
    urls = slack_webhook_urls()
    if not urls:
        return
    payload = {
        'text': text,
        'username': username,
        'mrkdwn': True,
    }
    for url in urls:
        try:
            requests.post(url, json=payload, timeout=8)
        except Exception as exc:
            logger.warning('Slack webhook post failed: %s', exc)


def notify_hw_error_change(
    *,
    chassis: KeysightChassis,
    node_name: str = '',
    reported: bool,
    notes: str = '',
    actor: str = '',
) -> None:
    """Announce a chassis/node HW flag being set or cleared (no-op without webhooks)."""
    if not slack_webhook_urls():
        return
    ch_label = chassis.hostname or chassis.ip_address
    target = f'{ch_label} / `{node_name}`' if node_name else ch_label
    action = 'flagged' if reported else 'cleared'
    who = f' by {actor}' if actor else ''
    lines = [f'Hardware issue *{action}*{who}: {target}']
    if reported and notes:
        lines.append(f'Notes: {notes}')
    url = _chassis_detail_url(chassis.id)
    if url:
        lines.append(f'<{url}|View chassis>')
    total = len(collect_hw_error_report())
    lines.append(f'Total flagged units: {total}')
    post_slack_message('\n'.join(lines))
