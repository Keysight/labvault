from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from connect.password_policy import (
    set_user_password,
    uses_relaxed_password_policy,
    validate_password_for_user,
)


class PasswordPolicyTests(TestCase):
    def test_admin_not_locked_by_default(self):
        User = get_user_model()
        admin = User(username='admin')
        self.assertFalse(uses_relaxed_password_policy(admin))

    @override_settings(LABVAULT_PASSWORD_LOCKED_USERNAMES=frozenset({'admin'}))
    def test_admin_uses_relaxed_policy_when_locked(self):
        User = get_user_model()
        admin = User(username='admin')
        self.assertTrue(uses_relaxed_password_policy(admin))

    @override_settings(LABVAULT_PASSWORD_LOCKED_USERNAMES=frozenset({'admin'}))
    def test_short_password_allowed_for_admin(self):
        User = get_user_model()
        admin = User.objects.create_user(username='admin', password='old')
        validate_password_for_user('admin', admin)  # 5 chars, same as username — allowed

    @override_settings(LABVAULT_PASSWORD_LOCKED_USERNAMES=frozenset({'admin'}))
    def test_short_password_rejected_for_mgmt(self):
        User = get_user_model()
        mgmt = User.objects.create_user(username='mgmt', password='old')
        with self.assertRaises(ValidationError):
            validate_password_for_user('mgmt', mgmt)

    @override_settings(LABVAULT_PASSWORD_LOCKED_USERNAMES=frozenset({'admin'}))
    def test_set_user_password_admin_short(self):
        User = get_user_model()
        admin = User.objects.create_user(username='admin', password='old')
        set_user_password(admin, 'abcde')
        admin.refresh_from_db()
        self.assertTrue(admin.check_password('abcde'))
