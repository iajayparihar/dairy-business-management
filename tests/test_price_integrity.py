
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.businesses.models import Business
from apps.products.models import Product, ProductCategory, ProductPrice


class PriceOverlapTests(TestCase):
    def setUp(self):
        self.business_a = Business.objects.create(
            name="Dairy A",
            slug="dairy-a",
        )

        category = ProductCategory.objects.create(
            business=self.business_a,
            name="Milk",
        )

        self.product_a = Product.objects.create(
            business=self.business_a,
            category=category,
            sku="MILK-001",
            name="Cow Milk",
            unit=Product.Unit.LITRE,
        )

    def test_overlapping_prices_are_rejected(self):
        start = datetime(2026, 1, 1, tzinfo=datetime_timezone.utc)
        middle = datetime(2026, 1, 10, tzinfo=datetime_timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=datetime_timezone.utc)

        ProductPrice.objects.create(
            business=self.business_a,
            product=self.product_a,
            price_type=ProductPrice.PriceType.SELLING,
            unit_price=Decimal("50.00"),
            effective_from=start,
            effective_to=end,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductPrice.objects.create(
                    business=self.business_a,
                    product=self.product_a,
                    price_type=ProductPrice.PriceType.SELLING,
                    unit_price=Decimal("55.00"),
                    effective_from=middle,
                    effective_to=None,
                )
