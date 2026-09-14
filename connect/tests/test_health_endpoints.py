"""Unauthenticated health probes."""
from django.test import TestCase


class HealthEndpointTests(TestCase):
    def test_live_ok(self):
        r = self.client.get("/health/live")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("status"), "live")

    def test_live_trailing_slash_not_required_route(self):
        r = self.client.get("/health/live/")
        self.assertEqual(r.status_code, 404)

    def test_ready_reports_status(self):
        r = self.client.get("/health/ready")
        self.assertIn(r.status_code, (200, 503))
        self.assertIn(r.json().get("status"), ("ready", "not_ready"))
