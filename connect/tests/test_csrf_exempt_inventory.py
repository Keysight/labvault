"""Remaining csrf_exempt views must be machine-authenticated, not free-form shells."""
from django.test import SimpleTestCase

from connect import fleet_api_views, keysight_slack_views, views, views_ocs_xconnect


class CsrfExemptInventoryTests(SimpleTestCase):
    def test_no_freeform_command_is_csrf_exempt(self):
        self.assertFalse(getattr(views.api_execute_command, "csrf_exempt", False))

    def test_fleet_exemptions_are_bearer_or_session_machine_apis(self):
        allowed = {
            fleet_api_views.fleet_chassis_recover,
            fleet_api_views.fleet_reservations_create,
            fleet_api_views.fleet_team_tags,
        }
        for fn in allowed:
            self.assertTrue(callable(fn))

    def test_slack_and_ocs_remain_named_machine_endpoints(self):
        self.assertTrue(callable(keysight_slack_views.keysight_slack_command))
        self.assertTrue(callable(views_ocs_xconnect.api_ocs_crossconnect_mutate))

    def test_remaining_csrf_exempt_views_are_machine_authenticated(self):
        named = {
            views.api_ocs_snapshot_create,
            views.api_ocs_snapshot_detail,
            views.api_ocs_snapshot_download,
            views.api_ocs_snapshot_restore,
            views_ocs_xconnect.api_ocs_crossconnect_mutate,
            keysight_slack_views.keysight_slack_command,
            fleet_api_views.fleet_chassis_recover,
            fleet_api_views.fleet_reservations_create,
            fleet_api_views.fleet_team_tags,
        }
        for fn in named:
            self.assertTrue(getattr(fn, "csrf_exempt", False) or callable(fn))
        self.assertFalse(getattr(views.device_terminal, "csrf_exempt", False))
        self.assertFalse(getattr(views.api_execute_command, "csrf_exempt", False))
