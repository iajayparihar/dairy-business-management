
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.businesses.models import Business
from apps.products.models import Product, ProductCategory


class PhaseOneIntegrityTests(TestCase):
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

    def test_soft_deleted_product_is_preserved(self):
        product_id = self.product_a.pk

        self.product_a.soft_delete()

        self.assertFalse(
            Product.objects.filter(pk=product_id).exists()
        )
        self.assertTrue(
            Product.all_objects.filter(
                pk=product_id,
                is_deleted=True,
            ).exists()
        )

    def test_duplicate_sku_in_same_business_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Product.objects.create(
                    business=self.business_a,
                    category=self.category_a,
                    sku="MILK-001",
                    name="Duplicate Milk",
                    unit=Product.Unit.LITRE,
                )

    def test_same_sku_is_allowed_in_another_business(self):
        category_b = ProductCategory.objects.create(
            business=self.business_b,
            name="Milk",
        )

        product_b = Product.objects.create(
            business=self.business_b,
            category=category_b,
            sku="MILK-001",
            name="Cow Milk",
            unit=Product.Unit.LITRE,
        )

        self.assertNotEqual(
            self.product_a.business_id,
            product_b.business_id,
        )

    def test_product_cannot_reference_another_business_category(self):
        category_b = ProductCategory.objects.create(
            business=self.business_b,
            name="Dairy Products",
        )

        invalid_product = Product(
            business=self.business_a,
            category=category_b,
            sku="MILK-002",
            name="Invalid Product",
            unit=Product.Unit.LITRE,
        )

        with self.assertRaises(ValidationError):
            invalid_product.full_clean()
