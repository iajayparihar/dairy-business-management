
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.services import create_business
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.products.models import Product, ProductPrice

User = get_user_model()


class DeliveryAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="delivery_api_owner",
            password="test-password-123",
        )

        self.business = create_business(
            name="Delivery API Dairy",
            slug="delivery-api-dairy",
            owner=self.user,
        )

        self.customer = Customer.objects.create(
            business=self.business,
            customer_code="CUST-001",
            name="Customer One",
        )

        self.product = Product.objects.create(
            business=self.business,
            sku="MILK-001",
            name="Cow Milk",
            unit=Product.Unit.LITRE,
        )

        ProductPrice.objects.create(
            business=self.business,
            product=self.product,
            price_type=ProductPrice.PriceType.SELLING,
            unit_price=Decimal("50.00"),
            effective_from=timezone.now() - timedelta(days=30),
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.url = reverse(
            "deliveries:delivery-list-create",
            kwargs={"business_id": self.business.pk},
        )

    def test_create_delivery_returns_201(self):
        response = self.client.post(
            self.url,
            {
                "customer_id": str(self.customer.pk),
                "product_id": str(self.product.pk),
                "delivered_at": timezone.now().isoformat(),
                "quantity": "2.000",
                "notes": "Morning delivery",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            Delivery.objects.filter(business=self.business).count(),
            1,
        )

    def test_create_delivery_uses_server_side_price(self):
        response = self.client.post(
            self.url,
            {
                "customer_id": str(self.customer.pk),
                "product_id": str(self.product.pk),
                "quantity": "2.000",
                "line_total": "2.00",  # not an input field: refused, never trusted
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            self.url,
            {
                "customer_id": str(self.customer.pk),
                "product_id": str(self.product.pk),
                "quantity": "2.000",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Decimal(response.data["unit_price"]), Decimal("50.00"))
        self.assertEqual(Decimal(response.data["line_total"]), Decimal("100.00"))

    def test_client_cannot_set_price_of_normal_product(self):
        response = self.client.post(
            self.url,
            {
                "customer_id": str(self.customer.pk),
                "product_id": str(self.product.pk),
                "quantity": "2.000",
                "unit_price": "1.00",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Delivery.objects.exists())

    def test_list_deliveries_returns_200(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["results"], [])

    def test_unauthenticated_user_cannot_list_deliveries(self):
        self.client.force_authenticate(user=None)

        response = self.client.get(self.url)

        self.assertIn(response.status_code, [401, 403])

    def test_foreign_customer_cannot_be_used(self):
        other_business = create_business(
            name="Another Dairy",
            slug="another-dairy",
            owner=self.user,
        )

        other_customer = Customer.objects.create(
            business=other_business,
            customer_code="FOREIGN-001",
            name="Foreign Customer",
        )

        response = self.client.post(
            self.url,
            {
                "customer_id": str(other_customer.pk),
                "product_id": str(self.product.pk),
                "delivered_at": timezone.now().isoformat(),
                "quantity": "1.000",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            Delivery.objects.filter(business=self.business).count(),
            0,
        )
