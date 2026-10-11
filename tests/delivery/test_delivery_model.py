
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.businesses.models import Business
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.products.models import Product


class DeliveryModelTests(TestCase):
    def setUp(self):
        self.business_a = Business.objects.create(
            name="Delivery Dairy A",
            slug="delivery-dairy-a",
        )

        self.business_b = Business.objects.create(
            name="Delivery Dairy B",
            slug="delivery-dairy-b",
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

        self.product_a = Product.objects.create(
            business=self.business_a,
            sku="MILK-001",
            name="Cow Milk",
            unit=Product.Unit.LITRE,
        )

        self.product_b = Product.objects.create(
            business=self.business_b,
            sku="MILK-001",
            name="Buffalo Milk",
            unit=Product.Unit.LITRE,
        )

    def test_delivery_calculates_line_total(self):
        delivery = Delivery.objects.create(
            business=self.business_a,
            customer=self.customer_a,
            product=self.product_a,
            delivery_date=date(2026, 10, 11),
            quantity=Decimal("2.500"),
            unit_price=Decimal("50.00"),
        )

        self.assertEqual(
            delivery.line_total,
            Decimal("125.00"),
        )

    def test_delivery_preserves_price_snapshot(self):
        delivery = Delivery.objects.create(
            business=self.business_a,
            customer=self.customer_a,
            product=self.product_a,
            delivery_date=date(2026, 10, 11),
            quantity=Decimal("2.000"),
            unit_price=Decimal("50.00"),
        )

        self.product_a.name = "Updated Cow Milk"
        self.product_a.save(update_fields=["name"])

        delivery.refresh_from_db()

        self.assertEqual(
            delivery.unit_price,
            Decimal("50.00"),
        )
        self.assertEqual(
            delivery.line_total,
            Decimal("100.00"),
        )

    def test_delivery_rejects_foreign_business_customer(self):
        delivery = Delivery(
            business=self.business_a,
            customer=self.customer_b,
            product=self.product_a,
            delivery_date=date(2026, 10, 11),
            quantity=Decimal("1.000"),
            unit_price=Decimal("50.00"),
        )

        with self.assertRaises(ValidationError):
            delivery.full_clean()

    def test_delivery_rejects_foreign_business_product(self):
        delivery = Delivery(
            business=self.business_a,
            customer=self.customer_a,
            product=self.product_b,
            delivery_date=date(2026, 10, 11),
            quantity=Decimal("1.000"),
            unit_price=Decimal("50.00"),
        )

        with self.assertRaises(ValidationError):
            delivery.full_clean()

    def test_zero_quantity_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Delivery.objects.create(
                    business=self.business_a,
                    customer=self.customer_a,
                    product=self.product_a,
                    delivery_date=date(2026, 10, 11),
                    quantity=Decimal("0.000"),
                    unit_price=Decimal("50.00"),
                )

    def test_delivery_can_be_soft_deleted_and_retained(self):
        delivery = Delivery.objects.create(
            business=self.business_a,
            customer=self.customer_a,
            product=self.product_a,
            delivery_date=date(2026, 10, 11),
            quantity=Decimal("1.000"),
            unit_price=Decimal("50.00"),
        )

        delivery.soft_delete()

        self.assertFalse(
            Delivery.objects.filter(pk=delivery.pk).exists()
        )
        self.assertTrue(
            Delivery.all_objects.filter(
                pk=delivery.pk,
                is_deleted=True,
            ).exists()
        )
