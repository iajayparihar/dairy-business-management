
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.permissions import IsBusinessMember


User = get_user_model()


class IsBusinessMemberTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.permission = IsBusinessMember()

        self.business = Business.objects.create(
            name="Dairy One",
            slug="dairy-one",
        )
        self.other_business = Business.objects.create(
            name="Dairy Two",
            slug="dairy-two",
        )

        self.superuser = User.objects.create_superuser(
            username="platform-admin",
            email="admin@example.com",
            password="Test-Only-Password-123!",
        )
        self.owner = User.objects.create_user(
            username="shop-owner",
            password="Test-Only-Password-123!",
        )
        self.staff = User.objects.create_user(
            username="shop-staff",
            password="Test-Only-Password-123!",
        )

        BusinessMembership.objects.create(
            business=self.business,
            user=self.owner,
            role=BusinessMembership.Role.OWNER,
            is_active=True,
        )
        BusinessMembership.objects.create(
            business=self.business,
            user=self.staff,
            role=BusinessMembership.Role.STAFF,
            is_active=True,
        )

    def make_request(self, user):
        request = self.factory.get("/")
        request.user = user
        return request

    def make_view(self, business, required_roles=None):
        return SimpleNamespace(
            kwargs={"business_id": business.pk},
            required_business_roles=required_roles,
        )

    def test_superuser_can_access_business_without_membership(self):
        request = self.make_request(self.superuser)
        view = self.make_view(self.other_business)

        self.assertTrue(
            self.permission.has_permission(request, view)
        )
        self.assertEqual(request.business.pk, self.other_business.pk)
        self.assertIsNone(request.business_membership)

    def test_superuser_can_pass_owner_manager_role_check(self):
        request = self.make_request(self.superuser)
        view = self.make_view(
            self.other_business,
            required_roles=[
                BusinessMembership.Role.OWNER,
                BusinessMembership.Role.MANAGER,
            ],
        )

        self.assertTrue(
            self.permission.has_permission(request, view)
        )

    def test_superuser_object_access_stays_in_url_business(self):
        request = self.make_request(self.superuser)
        view = self.make_view(self.business)

        self.assertTrue(
            self.permission.has_permission(request, view)
        )

        # An object from the URL's business should be allowed.
        own_object = SimpleNamespace(business_id=self.business.pk)
        self.assertTrue(
            self.permission.has_object_permission(
                request, view, own_object
            )
        )

        # An object from another business must be denied.
        other_object = SimpleNamespace(
            business_id=self.other_business.pk
        )
        self.assertFalse(
            self.permission.has_object_permission(
                request, view, other_object
            )
        )


    def test_normal_owner_can_access_own_business(self):
        request = self.make_request(self.owner)
        view = self.make_view(self.business)

        self.assertTrue(
            self.permission.has_permission(request, view)
        )

    def test_normal_staff_cannot_perform_owner_only_action(self):
        request = self.make_request(self.staff)
        view = self.make_view(
            self.business,
            required_roles=[
                BusinessMembership.Role.OWNER,
                BusinessMembership.Role.MANAGER,
            ],
        )

        self.assertFalse(
            self.permission.has_permission(request, view)
        )

    def test_normal_user_cannot_access_business_without_membership(self):
        request = self.make_request(self.owner)
        view = self.make_view(self.other_business)

        self.assertFalse(
            self.permission.has_permission(request, view)
        )

    def test_superuser_cannot_access_inactive_business(self):
        self.other_business.is_active = False
        self.other_business.save(update_fields=["is_active"])

        request = self.make_request(self.superuser)
        view = self.make_view(self.other_business)

        self.assertFalse(
            self.permission.has_permission(request, view)
        )
