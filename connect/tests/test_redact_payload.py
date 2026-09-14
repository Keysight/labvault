from django.test import SimpleTestCase

from connect.labvault_cli.redact import redact_payload


class RedactPayloadTests(SimpleTestCase):
    def test_nested_secrets_and_url_userinfo(self):
        payload = {
            "ok": True,
            "password": "secret",
            "nested": {"api_key": "abc", "note": "keep"},
            "items": [{"token": "t", "name": "demo-api"}],
            "url": "https://user:hunter2@example.com/path",
        }
        out = redact_payload(payload)
        self.assertEqual(out["password"], "***REDACTED***")
        self.assertEqual(out["nested"]["api_key"], "***REDACTED***")
        self.assertEqual(out["nested"]["note"], "keep")
        self.assertEqual(out["items"][0]["token"], "***REDACTED***")
        self.assertEqual(out["items"][0]["name"], "demo-api")
        self.assertIn("***:***@", out["url"])
        self.assertNotIn("hunter2", out["url"])
