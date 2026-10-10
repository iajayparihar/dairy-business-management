from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.businesses.models import Business
from apps.products.models import Product, ProductCategory, ProductPrice


class ProductModelTests(TestCase):
    def setUp(self):
        self.business_a = Business.objects.create(
            name="Dairy A",
            slug="dairy-a",
        )
        self.business_b = Business.objects.create(
            name="Dairy B",
            slug="dairy-b",
        )

        self.category_a = ProductCategory.objects.create(
            business=self.business_a,
            name="Milk",
        )

        self.product_a = Product.objects.create(
            business=self.business_a,
            category=self.category_a,
            sku="MILK-001",
            name="Cow Milk",
            unit=Product.Unit.LITRE,
        )

    def test_product_category_must_belong_to_same_business(self):
        category_b = ProductCategory.objects.create(
            business=self.business_b,
            name="Milk",
        )

        product = Product(
            business=self.business_a,
            category=category_b,
            sku="MILK-002",
            name="Buffalo Milk",
            unit=Product.Unit.LITRE,
        )

        with self.assertRaises(ValidationError):
            product.full_clean()

    def test_product_price_must_belong_to_same_business(self):
        price = ProductPrice(
            business=self.business_b,
            product=self.product_a,
            price_type=ProductPrice.PriceType.SELLING,
            unit_price=Decimal("60.00"),
            effective_from="2026-10-01T00:00:00+05:30",
        )

        with self.assertRaises(ValidationError):
            price.full_clean()