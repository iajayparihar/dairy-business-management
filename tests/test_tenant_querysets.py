
from django.test import TestCase

from apps.businesses.models import Business
from apps.common.querysets import scope_to_business
from apps.customers.models import Customer
from apps.products.models import Product, ProductCategory
from apps.suppliers.models import Supplier


class TenantQuerysetTests(TestCase):
    def setUp(self):
        self.business_a = Business.objects.create(
            name="Dairy A",
            slug="tenant-dairy-a",
        )
        self.business_b = Business.objects.create(
            name="Dairy B",
            slug="tenant-dairy-b",
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

        self.product_b = Product.objects.create(
            business=self.business_b,
            sku="MILK-001",
            name="Buffalo Milk",
            unit=Product.Unit.LITRE,
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

    def test_products_are_scoped_to_business(self):
        products = scope_to_business(
            Product.objects.all(),
            self.business_a,
        )

        self.assertEqual(
            list(products.values_list("pk", flat=True)),
            [self.product_a.pk],
        )

    def test_customers_are_scoped_to_business(self):
        customers = scope_to_business(
            Customer.objects.all(),
            self.business_a.pk,
        )

        self.assertEqual(
            list(customers.values_list("pk", flat=True)),
            [self.customer_a.pk],
        )

    def test_suppliers_are_scoped_to_business(self):
        suppliers = scope_to_business(
            Supplier.objects.all(),
            self.business_a,
        )

        self.assertEqual(
            list(suppliers.values_list("pk", flat=True)),
            [self.supplier_a.pk],
        )

    def test_missing_business_context_is_rejected(self):
        with self.assertRaises(ValueError):
            scope_to_business(Product.objects.all(), None)

    def test_foreign_business_product_is_not_returned(self):
        products = scope_to_business(
            Product.objects.all(),
            self.business_a,
        )

        self.assertFalse(
            products.filter(pk=self.product_b.pk).exists()
        )
