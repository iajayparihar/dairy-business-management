
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import (
    create_business,
    require_business_membership,
)

User = get_user_model()


class BusinessAuthorizationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="auth_owner",
            password="test-password-123",
        )
        self.staff = User.objects.create_user(
            username="auth_staff",
            password="test-password-123",
        )
        self.outsider = User.objects.create_user(
            username="auth_outsider",
            password="test-password-123",
        )

        self.business = create_business(
            name="Authorization Dairy",
            slug="authorization-dairy",
            owner=self.owner,
        )

        self.staff_membership = BusinessMembership.objects.create(
            business=self.business,
            user=self.staff,
            role=BusinessMembership.Role.STAFF,
            is_active=True,
        )

    def test_active_owner_can_access_business(self):
        membership = require_business_membership(
            user=self.owner,
            business_id=self.business.pk,
            allowed_roles=[BusinessMembership.Role.OWNER],
        )

        self.assertEqual(membership.user, self.owner)

    def test_staff_cannot_perform_owner_only_operation(self):
        with self.assertRaises(PermissionDenied):
            require_business_membership(
                user=self.staff,
                business_id=self.business.pk,
                allowed_roles=[BusinessMembership.Role.OWNER],
            )

    def test_user_from_another_business_is_denied(self):
        with self.assertRaises(PermissionDenied):
            require_business_membership(
                user=self.outsider,
                business_id=self.business.pk,
            )

    def test_inactive_membership_is_denied(self):
        self.staff_membership.is_active = False
        self.staff_membership.save(update_fields=["is_active"])

        with self.assertRaises(PermissionDenied):
            require_business_membership(
                user=self.staff,
                business_id=self.business.pk,
            )

    def test_inactive_business_is_denied(self):
        self.business.is_active = False
        self.business.save(update_fields=["is_active"])

        with self.assertRaises(PermissionDenied):
            require_business_membership(
                user=self.owner,
                business_id=self.business.pk,
            )
