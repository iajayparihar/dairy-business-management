
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.businesses.models import Business
from apps.businesses.services import create_business
from apps.products.models import Product, ProductPrice
from apps.products.services import change_product_price
from datetime import datetime, timedelta
from django.utils import timezone

from django.contrib.auth import get_user_model

User = get_user_model()

IST = ZoneInfo("Asia/Kolkata")


class ProductPriceServiceTests(TestCase):
    def setUp(self):
        self.owner_a = User.objects.create_user(
            username="price_owner_a",
            password="test-password-123",
        )
        self.owner_b = User.objects.create_user(
            username="price_owner_b",
            password="test-password-123",
        )

        self.business_a = create_business(
            name="Price Dairy A",
            slug="price-dairy-a",
            owner=self.owner_a,
        )
        self.business_b = create_business(
            name="Price Dairy B",
            slug="price-dairy-b",
            owner=self.owner_b,
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
            name="Cow Milk",
            unit=Product.Unit.LITRE,
        )

        self.change_at = (
            timezone.now() + timedelta(minutes=5)
        ).astimezone(IST).replace(microsecond=0)

    def test_price_change_closes_old_period_and_creates_new_period(self):
        old_price = ProductPrice.objects.create(
            business=self.business_a,
            product=self.product_a,
            price_type=ProductPrice.PriceType.SELLING,
            unit_price=Decimal("90.00"),
            effective_from=self.change_at - timedelta(hours=1),
            created_by=self.owner_a,
        )

        new_price = change_product_price(
            business_id=self.business_a.pk,
            product_id=self.product_a.pk,
            price_type=ProductPrice.PriceType.SELLING,
            unit_price="120.00",
            effective_from=self.change_at,
            user=self.owner_a,
        )

        old_price.refresh_from_db()

        self.assertEqual(old_price.effective_to, self.change_at)
        self.assertEqual(new_price.unit_price, Decimal("120.00"))
        self.assertEqual(new_price.effective_from, self.change_at)
        self.assertIsNone(new_price.effective_to)

    def test_price_change_does_not_affect_another_business(self):
        price_b = ProductPrice.objects.create(
            business=self.business_b,
            product=self.product_b,
            price_type=ProductPrice.PriceType.SELLING,
            unit_price=Decimal("85.00"),
            effective_from=self.change_at - timedelta(hours=1),
            created_by=self.owner_b,
        )

        change_product_price(
            business_id=self.business_a.pk,
            product_id=self.product_a.pk,
            price_type=ProductPrice.PriceType.SELLING,
            unit_price="120.00",
            effective_from=self.change_at,
            user=self.owner_a,
        )

        price_b.refresh_from_db()

        self.assertEqual(price_b.unit_price, Decimal("85.00"))
        self.assertIsNone(price_b.effective_to)

    def test_owner_cannot_change_another_business_product_price(self):
        # A clean validation error (400 over the API), not an unhandled
        # Product.DoesNotExist (500).
        with self.assertRaises(ValidationError) as caught:
            change_product_price(
                business_id=self.business_a.pk,
                product_id=self.product_b.pk,
                price_type=ProductPrice.PriceType.SELLING,
                unit_price="120.00",
                effective_from=self.change_at,
                user=self.owner_a,
            )
        self.assertIn("product", caught.exception.message_dict)

    def test_backdated_price_change_is_rejected(self):
        with self.assertRaises(ValidationError):
            change_product_price(
                business_id=self.business_a.pk,
                product_id=self.product_a.pk,
                price_type=ProductPrice.PriceType.SELLING,
                unit_price="120.00",
                effective_from=self.change_at - timedelta(hours=1),
                user=self.owner_a,
            )

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises(ValidationError):
            change_product_price(
                business_id=self.business_a.pk,
                product_id=self.product_a.pk,
                price_type=ProductPrice.PriceType.SELLING,
                unit_price="120.00",
                effective_from=self.change_at - timedelta(hours=1),
                user=self.owner_a,
            )
