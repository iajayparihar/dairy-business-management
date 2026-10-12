from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.businesses.models import Business, BusinessMembership
from apps.purchases.models import (
    MilkCollection,
    SupplierPurchase,
)
from apps.purchases.services import create_milk_collection
from apps.suppliers.models import Supplier, SupplierMilkRate
from apps.payments.selectors import get_supplier_balance

User = get_user_model()


class MilkCollectionServiceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username="milk_collection_owner",
            password="test-password",
        )
        cls.manager = User.objects.create_user(
            username="milk_collection_manager",
            password="test-password",
        )
        cls.staff = User.objects.create_user(
            username="milk_collection_staff",
            password="test-password",
        )

        cls.business = Business.objects.create(
            name="Milk Collection Dairy",
            slug="milk-collection-dairy",
        )

        BusinessMembership.objects.create(
            business=cls.business,
            user=cls.owner,
            role=BusinessMembership.Role.OWNER,
            is_active=True,
        )
        BusinessMembership.objects.create(
            business=cls.business,
            user=cls.manager,
            role=BusinessMembership.Role.MANAGER,
            is_active=True,
        )
        BusinessMembership.objects.create(
            business=cls.business,
            user=cls.staff,
            role=BusinessMembership.Role.STAFF,
            is_active=True,
        )

        cls.supplier = Supplier.objects.create(
            business=cls.business,
            supplier_code="MC-SUP-001",
            name="Milk Collection Supplier",
            is_active=True,
        )

    def setUp(self):
        self.start = timezone.now().replace(microsecond=0)

        self.rate = SupplierMilkRate.objects.create(
            business=self.business,
            supplier=self.supplier,
            milk_type=SupplierMilkRate.MilkType.COW,
            rate=Decimal("15.00"),
            effective_from=self.start - timedelta(hours=1),
            created_by=self.owner,
        )

    def create_collection(self, **overrides):
        data = {
            "business_id": self.business.id,
            "supplier_id": self.supplier.id,
            "collection_number": "MC-001",
            "milk_type": MilkCollection.MilkType.COW,
            "collected_at": self.start,
            "litres": "10",
            "fat": "9",
            "user": self.owner,
        }
        data.update(overrides)

        return create_milk_collection(**data)

    # ---------------------------------------------------------
    # Milk collection creation and amount calculation
    # ---------------------------------------------------------

    def test_collection_saves_correct_amount_and_rate_snapshot(self):
        collection = self.create_collection()

        self.assertEqual(collection.litres, Decimal("10"))
        self.assertEqual(collection.fat, Decimal("9"))
        self.assertEqual(collection.applied_rate, Decimal("15.00"))
        self.assertEqual(collection.amount, Decimal("1350.00"))
        self.assertEqual(collection.rate_record, self.rate)

    def test_duplicate_collection_number_is_rejected(self):
        self.create_collection()

        with self.assertRaises(ValidationError):
            self.create_collection()

        self.assertEqual(MilkCollection.objects.count(), 1)
        self.assertEqual(SupplierPurchase.objects.count(), 1)

    def test_collection_without_applicable_rate_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_collection(
                milk_type=MilkCollection.MilkType.BUFFALO,
            )

        self.assertEqual(MilkCollection.objects.count(), 0)
        self.assertEqual(SupplierPurchase.objects.count(), 0)

    def test_inactive_supplier_is_rejected(self):
        self.supplier.is_active = False
        self.supplier.save(update_fields=["is_active"])

        with self.assertRaises(ValidationError):
            self.create_collection()

        self.assertEqual(MilkCollection.objects.count(), 0)
        self.assertEqual(SupplierPurchase.objects.count(), 0)

    def test_negative_fat_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_collection(fat="-1")

        self.assertEqual(MilkCollection.objects.count(), 0)
        self.assertEqual(SupplierPurchase.objects.count(), 0)

    def test_zero_litres_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_collection(litres="0")

        self.assertEqual(MilkCollection.objects.count(), 0)
        self.assertEqual(SupplierPurchase.objects.count(), 0)

    def test_staff_cannot_create_collection(self):
        with self.assertRaises(PermissionDenied):
            self.create_collection(user=self.staff)

        self.assertEqual(MilkCollection.objects.count(), 0)
        self.assertEqual(SupplierPurchase.objects.count(), 0)

    def test_manager_can_create_collection(self):
        collection = self.create_collection(user=self.manager)

        self.assertEqual(collection.created_by, self.manager)
        self.assertEqual(MilkCollection.objects.count(), 1)

    # ---------------------------------------------------------
    # Historical rate snapshot
    # ---------------------------------------------------------

    def test_later_rate_change_does_not_change_saved_amount(self):
        collection = self.create_collection()

        change_time = self.start + timedelta(hours=1)

        self.rate.effective_to = change_time
        self.rate.save(update_fields=["effective_to"])

        SupplierMilkRate.objects.create(
            business=self.business,
            supplier=self.supplier,
            milk_type=SupplierMilkRate.MilkType.COW,
            rate=Decimal("20.00"),
            effective_from=change_time,
            created_by=self.owner,
        )

        collection.refresh_from_db()

        self.assertEqual(collection.applied_rate, Decimal("15.00"))
        self.assertEqual(collection.amount, Decimal("1350.00"))
        self.assertEqual(collection.rate_record, self.rate)

    # ---------------------------------------------------------
    # Purchase/payable integration
    # ---------------------------------------------------------

    def test_collection_creates_received_supplier_purchase(self):
        collection = self.create_collection()

        self.assertIsNotNone(collection.purchase)

        purchase = collection.purchase
        purchase.refresh_from_db()

        self.assertEqual(purchase.business, self.business)
        self.assertEqual(purchase.supplier, self.supplier)
        self.assertEqual(purchase.purchase_number, "MILK-MC-001")
        self.assertEqual(
            purchase.status,
            SupplierPurchase.Status.RECEIVED,
        )
        self.assertEqual(
            purchase.total_amount,
            Decimal("1350.00"),
        )

    def test_collection_amount_is_counted_once_in_supplier_balance(self):
        collection = self.create_collection()

        balance = get_supplier_balance(
            business_id=self.business.id,
            supplier_id=self.supplier.id,
        )

        self.assertEqual(
            balance["total_purchases"],
            Decimal("1350.00"),
        )
        self.assertEqual(
            balance["allocated_payments"],
            Decimal("0.00"),
        )
        self.assertEqual(
            balance["outstanding_amount"],
            Decimal("1350.00"),
        )

        # One collection must create exactly one payable purchase.
        self.assertEqual(
            SupplierPurchase.objects.filter(
                business=self.business,
                supplier=self.supplier,
            ).count(),
            1,
        )

        self.assertEqual(
            collection.purchase.total_amount,
            collection.amount,
        )

    def test_failed_collection_does_not_create_partial_payable(self):
        with self.assertRaises(ValidationError):
            self.create_collection(
                collection_number="",
            )

        self.assertEqual(MilkCollection.objects.count(), 0)
        self.assertEqual(SupplierPurchase.objects.count(), 0)

    def test_new_rate_does_not_change_existing_purchase_amount(self):
        collection = self.create_collection()
        purchase = collection.purchase

        change_time = self.start + timedelta(hours=1)

        self.rate.effective_to = change_time
        self.rate.save(update_fields=["effective_to"])

        SupplierMilkRate.objects.create(
            business=self.business,
            supplier=self.supplier,
            milk_type=SupplierMilkRate.MilkType.COW,
            rate=Decimal("20.00"),
            effective_from=change_time,
            created_by=self.owner,
        )

        collection.refresh_from_db()
        purchase.refresh_from_db()

        self.assertEqual(collection.applied_rate, Decimal("15.00"))
        self.assertEqual(collection.amount, Decimal("1350.00"))
        self.assertEqual(purchase.total_amount, Decimal("1350.00"))

    def test_different_collection_numbers_create_separate_payables(self):
        first = self.create_collection(
            collection_number="MC-101",
        )
        second = self.create_collection(
            collection_number="MC-102",
            collected_at=self.start + timedelta(minutes=5),
        )

        self.assertNotEqual(first.purchase_id, second.purchase_id)
        self.assertEqual(SupplierPurchase.objects.count(), 2)

        balance = get_supplier_balance(
            business_id=self.business.id,
            supplier_id=self.supplier.id,
        )

        self.assertEqual(
            balance["total_purchases"],
            Decimal("2700.00"),
        )
        self.assertEqual(
            balance["outstanding_amount"],
            Decimal("2700.00"),
        )