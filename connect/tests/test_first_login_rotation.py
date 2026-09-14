"""Bootstrap admin can rotate password in the UI (customer SKU default)."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from connect.models import APIToken


class FirstLoginRotationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "admin", "admin@localhost", "labvault!"
        )
        self.client.login(username="admin", password="labvault!")

    def test_password_link_allowed_and_form_renders(self):
        dash = self.client.get("/dashboard/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, reverse("password_change"))
        r = self.client.get(reverse("password_change"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Update password")

    def test_admin_can_change_password(self):
        r = self.client.post(
            reverse("password_change"),
            {
                "old_password": "labvault!",
                "new_password1": "RotateNow9!",
                "new_password2": "RotateNow9!",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("RotateNow9!"))

    def test_create_and_revoke_api_token(self):
        APIToken.objects.create(
            user=self.user, name="demo-api", token="labvault-default-api-token"
        )
        r = self.client.post(reverse("create_api_token"), {"name": "ops-token"})
        self.assertEqual(r.status_code, 302)
        created = APIToken.objects.get(user=self.user, name="ops-token")
        self.assertTrue(created.enabled)
        self.assertNotEqual(created.token, "labvault-default-api-token")
        row = APIToken.objects.get(user=self.user, name="demo-api")
        revoke = self.client.post(reverse("revoke_api_token", args=[row.pk]))
        self.assertEqual(revoke.status_code, 200)
        row.refresh_from_db()
        self.assertFalse(row.enabled)
