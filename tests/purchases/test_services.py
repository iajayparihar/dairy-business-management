
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError,PermissionDenied
from django.test import TestCase
from django.utils import timezone

from apps.businesses.models import Business, BusinessMembership
from apps.inventory.models import StockMovement
from apps.products.models import Product
from apps.purchases.models import SupplierPurchase
from apps.purchases.services import create_purchase, receive_purchase
from apps.suppliers.models import Supplier

class PurchaseServiceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="dairy_owner",
            password="test-password",
        )

        self.business = Business.objects.create(
            name="Test Dairy",
            slug="test-dairy",
        )

        BusinessMembership.objects.create(
            business=self.business,
            user=self.user,
            role="OWNER",
            is_active=True,
        )

        self.supplier = Supplier.objects.create(
            business=self.business,
            supplier_code="SUP001",
            name="Milk Supplier",
        )

        self.product = Product.objects.create(
            business=self.business,
            sku="MILK001",
            name="Cow Milk",
            unit="LITRE",
            track_stock=True,
        )

    def create_draft_purchase(self):
        return create_purchase(
            business_id=self.business.pk,
            supplier_id=self.supplier.pk,
            purchase_number="PUR001",
            purchased_at=timezone.now(),
            lines=[
                {
                    "product_id": self.product.pk,
                    "quantity": "10.000",
                    "unit_price": "50.00",
                }
            ],
            user=self.user,
        )

    def test_create_purchase_as_draft(self):
        purchase = self.create_draft_purchase()

        self.assertEqual(purchase.status, SupplierPurchase.Status.DRAFT)
        self.assertEqual(purchase.total_amount, Decimal("500.00"))
        self.assertEqual(purchase.lines.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_receiving_purchase_adds_one_stock_movement(self):
        purchase = self.create_draft_purchase()

        receive_purchase(purchase_id=purchase.pk, user=self.user)

        purchase.refresh_from_db()

        self.assertEqual(
            purchase.status,
            SupplierPurchase.Status.RECEIVED,
        )
        self.assertEqual(
            StockMovement.objects.filter(
                purchase_line__purchase=purchase,
                movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
            ).count(),
            1,
        )


    def test_purchase_cannot_be_received_twice(self):
        purchase = self.create_draft_purchase()

        receive_purchase(purchase_id=purchase.pk, user=self.user)

        with self.assertRaises(ValidationError):
            receive_purchase(purchase_id=purchase.pk, user=self.user)

        self.assertEqual(
            StockMovement.objects.filter(
                purchase_line__purchase=purchase,
                movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
            ).count(),
            1,
        )


    def test_staff_cannot_create_purchase(self):
        User = get_user_model()
        staff_user = User.objects.create_user(
            username="dairy_staff",
            password="test-password",
        )

        BusinessMembership.objects.create(
            business=self.business,
            user=staff_user,
            role="STAFF",
            is_active=True,
        )

        with self.assertRaises(PermissionDenied):
            create_purchase(
                business_id=self.business.pk,
                supplier_id=self.supplier.pk,
                purchase_number="PUR002",
                purchased_at=timezone.now(),
                lines=[
                    {
                        "product_id": self.product.pk,
                        "quantity": "2.000",
                        "unit_price": "50.00",
                    }
                ],
                user=staff_user,
            )

    def test_purchase_rejects_product_from_another_business(self):
        other_business = Business.objects.create(
            name="Other Dairy",
            slug="other-dairy",
        )

        other_product = Product.objects.create(
            business=other_business,
            sku="MILK002",
            name="Other Milk",
            unit="LITRE",
            track_stock=True,
        )

        with self.assertRaises(Product.DoesNotExist):
            create_purchase(
                business_id=self.business.pk,
                supplier_id=self.supplier.pk,
                purchase_number="PUR003",
                purchased_at=timezone.now(),
                lines=[
                    {
                        "product_id": other_product.pk,
                        "quantity": "2.000",
                        "unit_price": "50.00",
                    }
                ],
                user=self.user,
            )
