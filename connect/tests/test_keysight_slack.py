"""Slack bot helpers for Keysight hardware error reporting."""

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from connect.keysight_hw_errors import collect_hw_error_report
from connect.keysight_slack import (
    format_hw_errors_text,
    handle_slack_command,
    verify_slack_signature,
)
from connect.models import KeysightBmcEndpoint, KeysightChassis


class SlackCommandTests(TestCase):
    def test_help(self):
        resp = handle_slack_command('')
        self.assertEqual(resp['response_type'], 'ephemeral')
        self.assertIn('hw-errors', resp['text'])

    def test_hw_errors_empty(self):
        resp = handle_slack_command('bad')
        self.assertIn('No reported hardware issues', resp['text'])

    def test_count(self):
        resp = handle_slack_command('count')
        self.assertIn('0 unit', resp['text'])


class CollectHwErrorReportTests(TestCase):
    def test_lists_node_and_chassis(self):
        ch = KeysightChassis.objects.create(
            ip_address='10.9.9.1', username='u', password='p',
            chassis_type='aps_m1010', hardware_error_reported=True, notes='mgmt bad',
        )
        KeysightBmcEndpoint.objects.create(
            hostname='bmc1', chassis=ch, node_name='cn-aps-o15-tw1',
            hardware_error_reported=True, hardware_error_notes='no qat',
        )
        rows = collect_hw_error_report()
        self.assertEqual(len(rows), 2)
        text = format_hw_errors_text(rows)
        self.assertIn('cn-aps-o15-tw1', text)
        self.assertIn('mgmt bad', text)


class SlackSignatureTests(SimpleTestCase):
    @override_settings()
    def test_rejects_without_secret(self):
        rf = RequestFactory()
        req = rf.post('/api/slack/keysight/', data={'text': 'help'})
        self.assertFalse(verify_slack_signature(req))
