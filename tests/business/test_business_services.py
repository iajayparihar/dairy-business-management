
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import create_business

User = get_user_model()


class BusinessCreationServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="dairy_owner",
            password="test-password-123",
        )

    def test_business_is_created_with_initial_owner(self):
        business = create_business(
            name="Ajay Dairy",
            slug="ajay-dairy",
            owner=self.user,
        )

        self.assertTrue(
            Business.objects.filter(pk=business.pk).exists()
        )

        membership = BusinessMembership.objects.get(
            business=business,
            user=self.user,
        )

        self.assertEqual(
            membership.role,
            BusinessMembership.Role.OWNER,
        )
        self.assertTrue(membership.is_active)

    def test_failed_membership_creation_rolls_back_business(self):
        initial_count = Business.objects.count()

        with self.assertRaises(Exception):
            create_business(
                name="Rollback Dairy",
                slug="rollback-dairy",
                owner=self.user,
                invalid_field="invalid",
            )

        self.assertEqual(Business.objects.count(), initial_count)
