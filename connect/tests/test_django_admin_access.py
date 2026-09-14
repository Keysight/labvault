from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from connect.django_admin_access import (
    patch_django_admin_site_access,
    user_may_access_django_admin,
)


class DjangoAdminAccessHelpersTests(SimpleTestCase):
    @override_settings(LABVAULT_DJANGO_ADMIN_USERNAMES=frozenset({'godmode', 'backup'}))
    def test_allowed_usernames_from_settings(self):
        user = type('U', (), {'username': 'Backup', 'is_staff': True, 'is_active': True})()
        self.assertTrue(user_may_access_django_admin(user))

    def test_staff_alone_is_insufficient_when_not_listed(self):
        user = type('U', (), {'username': 'mgmt', 'is_staff': True, 'is_active': True})()
        self.assertFalse(user_may_access_django_admin(user))

    def test_shared_admin_without_staff_cannot_use_django_admin(self):
        user = type('U', (), {'username': 'admin', 'is_staff': False, 'is_active': True})()
        self.assertFalse(user_may_access_django_admin(user))


class DjangoAdminSitePermissionTests(TestCase):
    def setUp(self):
        patch_django_admin_site_access()
        self.factory = RequestFactory()
        User = get_user_model()
        self.godmode = User.objects.create_user(
            username='godmode',
            password='godmode',
            is_staff=True,
            is_superuser=True,
        )
        self.mgmt = User.objects.create_user(
            username='mgmt',
            password='mgmt',
            is_staff=True,
            is_superuser=True,
        )

    def test_godmode_has_admin_permission(self):
        request = self.factory.get('/admin/')
        request.user = self.godmode
        self.assertTrue(admin.site.has_permission(request))

    def test_mgmt_staff_superuser_denied_admin(self):
        request = self.factory.get('/admin/')
        request.user = self.mgmt
        self.assertFalse(admin.site.has_permission(request))

    def test_anonymous_denied(self):
        request = self.factory.get('/admin/')
        request.user = AnonymousUser()
        self.assertFalse(admin.site.has_permission(request))
