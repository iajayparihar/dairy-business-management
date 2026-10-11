
from types import SimpleNamespace

from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.businesses.models import Business
from apps.products.models import Product, ProductCategory
from apps.products.serializers import (
    ProductCategorySerializer,
    ProductSerializer,
)


class ProductSerializerTests(TestCase):
    def setUp(self):
        self.business_a = Business.objects.create(
            name="Serializer Dairy A",
            slug="serializer-dairy-a",
        )
        self.business_b = Business.objects.create(
            name="Serializer Dairy B",
            slug="serializer-dairy-b",
        )

        self.category_a = ProductCategory.objects.create(
            business=self.business_a,
            name="Milk",
        )
        self.category_b = ProductCategory.objects.create(
            business=self.business_b,
            name="Milk",
        )

        self.request_a = SimpleNamespace(
            business=self.business_a
        )
        self.context_a = {"request": self.request_a}

    def test_product_uses_business_from_request_context(self):
        serializer = ProductSerializer(
            data={
                "sku": "MILK-001",
                "name": "Cow Milk",
                "unit": Product.Unit.LITRE,
                "category": str(self.category_a.pk),
            },
            context=self.context_a,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        product = serializer.save()

        self.assertEqual(product.business, self.business_a)
        self.assertEqual(product.category, self.category_a)

    def test_product_cannot_use_foreign_business_category(self):
        serializer = ProductSerializer(
            data={
                "sku": "MILK-002",
                "name": "Buffalo Milk",
                "unit": Product.Unit.LITRE,
                "category": str(self.category_b.pk),
            },
            context=self.context_a,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("category", serializer.errors)

    def test_client_cannot_override_product_business(self):
        serializer = ProductSerializer(
            data={
                "business": str(self.business_b.pk),
                "sku": "MILK-003",
                "name": "Cow Milk",
                "unit": Product.Unit.LITRE,
                "category": str(self.category_a.pk),
            },
            context=self.context_a,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        product = serializer.save()

        self.assertEqual(product.business, self.business_a)

    def test_product_creation_requires_business_context(self):
        serializer = ProductSerializer(
            data={
                "sku": "MILK-004",
                "name": "Cow Milk",
                "unit": Product.Unit.LITRE,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

        with self.assertRaises(Exception):
            serializer.save()

    def test_category_uses_business_from_request_context(self):
        serializer = ProductCategorySerializer(
            data={
                "name": "Dairy Products",
                "description": "Milk and related products",
            },
            context=self.context_a,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        category = serializer.save()

        self.assertEqual(category.business, self.business_a)
