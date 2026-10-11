
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.businesses.services import create_business

User = get_user_model()


class BusinessPermissionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="permission_owner",
            password="test-password-123",
        )
        self.staff = User.objects.create_user(
            username="permission_staff",
            password="test-password-123",
        )

        self.business = create_business(
            name="Permission Dairy",
            slug="permission-dairy",
            owner=self.owner,
        )

        self.business_b = Business.objects.create(
            name="Other Dairy",
            slug="other-dairy",
        )

        BusinessMembership.objects.create(
            business=self.business,
            user=self.staff,
            role=BusinessMembership.Role.STAFF,
            is_active=True,
        )

        self.permission = IsBusinessMember()

    def make_request(self, user):
        return SimpleNamespace(user=user)

    def make_view(self, business_id, roles=None):
        return SimpleNamespace(
            kwargs={"business_id": str(business_id)},
            required_business_roles=roles,
        )

    def test_member_can_access_own_business(self):
        request = self.make_request(self.owner)
        view = self.make_view(self.business.pk)

        self.assertTrue(
            self.permission.has_permission(request, view)
        )

    def test_staff_is_denied_owner_only_access(self):
        request = self.make_request(self.staff)
        view = self.make_view(
            self.business.pk,
            roles=[BusinessMembership.Role.OWNER],
        )

        self.assertFalse(
            self.permission.has_permission(request, view)
        )

    def test_non_member_is_denied(self):
        request = self.make_request(self.staff)
        view = self.make_view(self.business_b.pk)

        self.assertFalse(
            self.permission.has_permission(request, view)
        )

    def test_missing_business_id_is_denied(self):
        request = self.make_request(self.owner)
        view = SimpleNamespace(
            kwargs={},
            required_business_roles=None,
        )

        self.assertFalse(
            self.permission.has_permission(request, view)
        )

    def test_cross_business_object_access_is_denied(self):
        request = self.make_request(self.owner)
        view = self.make_view(self.business.pk)

        self.assertTrue(
            self.permission.has_permission(request, view)
        )

        foreign_object = SimpleNamespace(
            business_id=self.business_b.pk
        )

        self.assertFalse(
            self.permission.has_object_permission(
                request, view, foreign_object
            )
        )
