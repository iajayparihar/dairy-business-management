
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import (
    create_business,
    update_membership,
)
from django.contrib.auth import get_user_model

User = get_user_model()


class OwnerProtectionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner_one",
            password="test-password-123",
        )

        self.business = create_business(
            name="Owner Protection Dairy",
            slug="owner-protection-dairy",
            owner=self.owner,
        )

        self.membership = BusinessMembership.objects.get(
            business=self.business,
            user=self.owner,
        )

    def test_last_owner_cannot_be_deactivated(self):
        with self.assertRaises(ValidationError):
            update_membership(
                membership_id=self.membership.pk,
                is_active=False,
            )

        self.membership.refresh_from_db()
        self.assertTrue(self.membership.is_active)

    def test_last_owner_cannot_be_demoted(self):
        with self.assertRaises(ValidationError):
            update_membership(
                membership_id=self.membership.pk,
                role=BusinessMembership.Role.MANAGER,
            )

        self.membership.refresh_from_db()
        self.assertEqual(
            self.membership.role,
            BusinessMembership.Role.OWNER,
        )

    def test_owner_can_be_deactivated_when_another_owner_exists(self):
        second_owner = User.objects.create_user(
            username="owner_two",
            password="test-password-123",
        )

        BusinessMembership.objects.create(
            business=self.business,
            user=second_owner,
            role=BusinessMembership.Role.OWNER,
            is_active=True,
        )

        updated_membership = update_membership(
            membership_id=self.membership.pk,
            is_active=False,
        )

        self.assertFalse(updated_membership.is_active)

        self.assertEqual(
            BusinessMembership.objects.filter(
                business=self.business,
                role=BusinessMembership.Role.OWNER,
                is_active=True,
            ).count(),
            1,
        )
