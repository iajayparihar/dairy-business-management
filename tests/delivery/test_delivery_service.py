
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import create_business
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.deliveries.services import create_delivery
from apps.products.models import Product, ProductPrice

IST = ZoneInfo("Asia/Kolkata")
User = get_user_model()


class DeliveryServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="delivery_service_owner",
            password="test-password-123",
        )

        self.business = create_business(
            name="Delivery Service Dairy",
            slug="delivery-service-dairy",
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
            effective_from=datetime(
                2026, 1, 1, tzinfo=IST
            ),
        )

    def test_delivery_uses_effective_selling_price(self):
        delivery = create_delivery(
            business_id=self.business.pk,
            customer_id=self.customer.pk,
            product_id=self.product.pk,
            delivered_at=datetime(
                2026, 10, 12, 12, 59,
                tzinfo=IST,
            ),
            quantity=Decimal("2.000"),
            user=self.user,
        )

        self.assertEqual(delivery.unit_price, Decimal("50.00"))
        self.assertEqual(delivery.line_total, Decimal("100.00"))

    def test_delivery_uses_new_price_after_price_change(self):
        ProductPrice.objects.filter(
            business=self.business,
            product=self.product,
        ).update(
            effective_to=datetime(
                2026, 1, 15, tzinfo=IST
            )
        )

        ProductPrice.objects.create(
            business=self.business,
            product=self.product,
            price_type=ProductPrice.PriceType.SELLING,
            unit_price=Decimal("55.00"),
            effective_from=datetime(
                2026, 1, 15, tzinfo=IST
            ),
        )

        delivery = create_delivery(
            business_id=self.business.pk,
            customer_id=self.customer.pk,
            product_id=self.product.pk,
            delivered_at=datetime(
                2026, 1, 20, tzinfo=IST
            ),
            quantity=Decimal("2.000"),
            user=self.user,
        )

        self.assertEqual(delivery.unit_price, Decimal("55.00"))
        self.assertEqual(delivery.line_total, Decimal("110.00"))

    def test_delivery_without_effective_price_is_rejected(self):
        with self.assertRaises(ValidationError):
            create_delivery(
                business_id=self.business.pk,
                customer_id=self.customer.pk,
                product_id=self.product.pk,
                delivered_at=datetime(
                    2025, 12, 1, tzinfo=IST
                ),
                quantity=Decimal("1.000"),
                user=self.user,
            )

        self.assertEqual(Delivery.objects.count(), 0)

    def test_delivery_rejects_foreign_customer(self):
        other_business = Business.objects.create(
            name="Other Delivery Dairy",
            slug="other-delivery-dairy",
        )

        other_customer = Customer.objects.create(
            business=other_business,
            customer_code="CUST-001",
            name="Foreign Customer",
        )

        with self.assertRaises(ValidationError):
            create_delivery(
                business_id=self.business.pk,
                customer_id=other_customer.pk,
                product_id=self.product.pk,
                delivered_at=datetime(
                    2026, 1, 10, tzinfo=IST
                ),
                quantity=Decimal("1.000"),
                user=self.user,
            )
