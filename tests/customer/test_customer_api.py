
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import create_business
from apps.customers.models import Customer

User = get_user_model()


class CustomerAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.owner = User.objects.create_user(
            username="customer_api_owner",
            password="test-password-123",
        )
        self.staff = User.objects.create_user(
            username="customer_api_staff",
            password="test-password-123",
        )
        self.outsider = User.objects.create_user(
            username="customer_api_outsider",
            password="test-password-123",
        )

        self.business_a = create_business(
            name="Customer API Dairy A",
            slug="customer-api-dairy-a",
            owner=self.owner,
        )
        self.business_b = Business.objects.create(
            name="Customer API Dairy B",
            slug="customer-api-dairy-b",
        )

        BusinessMembership.objects.create(
            business=self.business_a,
            user=self.staff,
            role=BusinessMembership.Role.STAFF,
            is_active=True,
        )

        self.customer_a = Customer.objects.create(
            business=self.business_a,
            customer_code="CUST-001",
            name="Customer A",
        )
        self.customer_b = Customer.objects.create(
            business=self.business_b,
            customer_code="CUST-001",
            name="Customer B",
        )

        self.list_url = reverse(
            "customers:customer-list-create",
            kwargs={"business_id": self.business_a.pk},
        )
        self.detail_url = reverse(
            "customers:customer-detail",
            kwargs={
                "business_id": self.business_a.pk,
                "pk": self.customer_a.pk,
            },
        )

    def test_owner_can_list_own_customers(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            str(response.data["results"][0]["id"]),
            str(self.customer_a.pk),
        )

    def test_outsider_cannot_list_customers(self):
        self.client.force_authenticate(user=self.outsider)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 403)

    def test_foreign_customer_is_not_retrievable(self):
        self.client.force_authenticate(user=self.owner)

        foreign_url = reverse(
            "customers:customer-detail",
            kwargs={
                "business_id": self.business_a.pk,
                "pk": self.customer_b.pk,
            },
        )

        response = self.client.get(foreign_url)

        self.assertEqual(response.status_code, 404)

    def test_owner_can_create_customer(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.post(
            self.list_url,
            {
                "customer_code": "CUST-002",
                "name": "New Customer",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            Customer.objects.filter(
                business=self.business_a,
                customer_code="CUST-002",
            ).exists()
        )

    def test_client_cannot_override_business(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.post(
            self.list_url,
            {
                "business": str(self.business_b.pk),
                "customer_code": "CUST-003",
                "name": "New Customer",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)

        customer = Customer.objects.get(
            business=self.business_a,
            customer_code="CUST-003",
        )
        self.assertEqual(customer.business, self.business_a)

    def test_staff_cannot_create_customer(self):
        self.client.force_authenticate(user=self.staff)

        response = self.client.post(
            self.list_url,
            {
                "customer_code": "CUST-004",
                "name": "Staff Created Customer",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_customer_delete_is_soft_delete(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.delete(self.detail_url)

        self.assertEqual(response.status_code, 204)
        self.assertTrue(
            Customer.all_objects.filter(
                pk=self.customer_a.pk,
                is_deleted=True,
            ).exists()
        )
