"""Change log report page renders unified ChangeLogEvent rows."""
from django.contrib.auth.models import User
from django.test import Client, TestCase

from connect.models import ChangeLogEvent


class ChangeLogReportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('changelog_user', password='test')
        self.client = Client()
        self.client.force_login(self.user)
        ChangeLogEvent.objects.create(
            event_type='status_down',
            target_kind='device',
            target_id=1,
            target_repr='test-switch',
            detail='probe failed',
        )

    def test_report_lists_events(self):
        r = self.client.get('/reports/changes/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Status, moves, and operations')
        self.assertContains(r, 'test-switch')
        self.assertNotContains(r, 'No config changes found')
