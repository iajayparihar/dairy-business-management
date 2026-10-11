
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import create_business
from apps.suppliers.models import Supplier

User = get_user_model()


class SupplierAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.owner = User.objects.create_user(
            username="supplier_api_owner",
            password="test-password-123",
        )
        self.staff = User.objects.create_user(
            username="supplier_api_staff",
            password="test-password-123",
        )
        self.outsider = User.objects.create_user(
            username="supplier_api_outsider",
            password="test-password-123",
        )

        self.business_a = create_business(
            name="Supplier API Dairy A",
            slug="supplier-api-dairy-a",
            owner=self.owner,
        )
        self.business_b = Business.objects.create(
            name="Supplier API Dairy B",
            slug="supplier-api-dairy-b",
        )

        BusinessMembership.objects.create(
            business=self.business_a,
            user=self.staff,
            role=BusinessMembership.Role.STAFF,
            is_active=True,
        )

        self.supplier_a = Supplier.objects.create(
            business=self.business_a,
            supplier_code="SUP-001",
            name="Supplier A",
        )
        self.supplier_b = Supplier.objects.create(
            business=self.business_b,
            supplier_code="SUP-001",
            name="Supplier B",
        )

        self.list_url = reverse(
            "suppliers:supplier-list-create",
            kwargs={"business_id": self.business_a.pk},
        )
        self.detail_url = reverse(
            "suppliers:supplier-detail",
            kwargs={
                "business_id": self.business_a.pk,
                "pk": self.supplier_a.pk,
            },
        )

    def test_owner_can_list_own_suppliers(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            str(response.data[0]["id"]),
            str(self.supplier_a.pk),
        )

    def test_outsider_cannot_list_suppliers(self):
        self.client.force_authenticate(user=self.outsider)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 403)

    def test_foreign_supplier_is_not_retrievable(self):
        self.client.force_authenticate(user=self.owner)

        foreign_url = reverse(
            "suppliers:supplier-detail",
            kwargs={
                "business_id": self.business_a.pk,
                "pk": self.supplier_b.pk,
            },
        )
        response = self.client.get(foreign_url)

        self.assertEqual(response.status_code, 404)

    def test_owner_can_create_supplier(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.post(
            self.list_url,
            {
                "supplier_code": "SUP-002",
                "name": "New Supplier",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            Supplier.objects.filter(
                business=self.business_a,
                supplier_code="SUP-002",
            ).exists()
        )

    def test_staff_cannot_create_supplier(self):
        self.client.force_authenticate(user=self.staff)

        response = self.client.post(
            self.list_url,
            {
                "supplier_code": "SUP-003",
                "name": "Staff Supplier",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_client_cannot_override_business(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.post(
            self.list_url,
            {
                "business": str(self.business_b.pk),
                "supplier_code": "SUP-004",
                "name": "New Supplier",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)

        supplier = Supplier.objects.get(
            business=self.business_a,
            supplier_code="SUP-004",
        )
        self.assertEqual(supplier.business, self.business_a)

    def test_supplier_delete_is_soft_delete(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.delete(self.detail_url)

        self.assertEqual(response.status_code, 204)
        self.assertTrue(
            Supplier.all_objects.filter(
                pk=self.supplier_a.pk,
                is_deleted=True,
            ).exists()
        )
