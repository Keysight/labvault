"""Slack slash-command endpoint for Keysight lab queries."""

from __future__ import annotations

import json
import logging

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .keysight_slack import handle_slack_command, verify_slack_signature

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def keysight_slack_command(request):
    """Slack slash command handler (configure Request URL in Slack app)."""
    if not verify_slack_signature(request):
        return HttpResponse('invalid signature', status=403)
    text = request.POST.get('text', '')
    try:
        payload = handle_slack_command(text)
    except Exception:
        logger.exception('Slack command failed')
        payload = {
            'response_type': 'ephemeral',
            'text': 'LabVault bot error — check server logs.',
        }
    return JsonResponse(payload)
