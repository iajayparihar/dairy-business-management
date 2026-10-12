from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.businesses.models import Business, BusinessMembership
from apps.purchases.models import MilkCollection
from apps.purchases.services import create_milk_collection
from apps.suppliers.models import Supplier, SupplierMilkRate
from django.core.exceptions import PermissionDenied
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

    def test_collection_without_applicable_rate_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_collection(
                milk_type=MilkCollection.MilkType.BUFFALO,
            )

        self.assertEqual(MilkCollection.objects.count(), 0)

    def test_inactive_supplier_is_rejected(self):
        self.supplier.is_active = False
        self.supplier.save(update_fields=["is_active"])

        with self.assertRaises(ValidationError):
            self.create_collection()

        self.assertEqual(MilkCollection.objects.count(), 0)

    def test_negative_fat_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_collection(fat="-1")

        self.assertEqual(MilkCollection.objects.count(), 0)

    def test_zero_litres_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_collection(litres="0")

        self.assertEqual(MilkCollection.objects.count(), 0)

    def test_staff_cannot_create_collection(self):
        with self.assertRaises(PermissionDenied):
            self.create_collection(user=self.staff)

        self.assertEqual(MilkCollection.objects.count(), 0)

    def test_later_rate_change_does_not_change_saved_amount(self):
        collection = self.create_collection()

        change_time = self.start + timedelta(hours=1)

        # Close the old rate at the future change time.
        self.rate.effective_to = change_time
        self.rate.save(update_fields=["effective_to"])

        # Create the new rate from the change time.
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