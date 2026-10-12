from datetime import datetime, timedelta
from decimal import Decimal
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from apps.businesses.models import Business, BusinessMembership
from apps.suppliers.models import Supplier, SupplierMilkRate
from apps.suppliers.services import create_supplier_milk_rate
from decimal import Decimal
from datetime import timedelta

from django.core.exceptions import ValidationError

from apps.suppliers.services import (
    calculate_milk_amount,
    calculate_supplier_milk_amount,
    resolve_supplier_milk_rate,
)



User = get_user_model()


class SupplierMilkRateServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username="milk_owner",
            password="test-password",
        )
        cls.manager = User.objects.create_user(
            username="milk_manager",
            password="test-password",
        )
        cls.staff = User.objects.create_user(
            username="milk_staff",
            password="test-password",
        )

        cls.business = Business.objects.create(
            name="Test Dairy",
            slug="test-dairy",
        )
        cls.other_business = Business.objects.create(
            name="Other Dairy",
            slug="other-dairy",
        )

        for user, role in (
            (cls.owner, BusinessMembership.Role.OWNER),
            (cls.manager, BusinessMembership.Role.MANAGER),
            (cls.staff, BusinessMembership.Role.STAFF),
        ):
            BusinessMembership.objects.create(
                business=cls.business,
                user=user,
                role=role,
                is_active=True,
            )

        cls.supplier = Supplier.objects.create(
            business=cls.business,
            supplier_code="SUP001",
            name="Test Supplier",
            is_active=True,
        )
        cls.other_supplier = Supplier.objects.create(
            business=cls.other_business,
            supplier_code="SUP001",
            name="Other Supplier",
            is_active=True,
        )

    def setUp(self):
        self.start = timezone.now().replace(microsecond=0)

    def create_rate(self, **overrides):
        data = {
            "business_id": self.business.id,
            "supplier_id": self.supplier.id,
            "milk_type": SupplierMilkRate.MilkType.COW,
            "rate": Decimal("15.00"),
            "effective_from": self.start,
            "user": self.owner,
        }
        data.update(overrides)
        return create_supplier_milk_rate(**data)

    def test_owner_can_create_cow_rate(self):
        rate = self.create_rate()

        self.assertEqual(rate.milk_type, "COW")
        self.assertEqual(rate.rate, Decimal("15.00"))
        self.assertEqual(rate.supplier, self.supplier)
        self.assertEqual(rate.business, self.business)
        self.assertEqual(rate.created_by, self.owner)

    def test_manager_can_create_buffalo_rate(self):
        rate = self.create_rate(
            milk_type=SupplierMilkRate.MilkType.BUFFALO,
            rate=Decimal("18.00"),
            user=self.manager,
        )

        self.assertEqual(rate.milk_type, "BUFFALO")
        self.assertEqual(rate.rate, Decimal("18.00"))

    def test_staff_cannot_create_rate(self):
        with self.assertRaises(ValidationError):
            self.create_rate(user=self.staff)

        self.assertEqual(SupplierMilkRate.objects.count(), 0)

    def test_supplier_from_another_business_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_rate(
                supplier_id=self.other_supplier.id,
            )

        self.assertEqual(SupplierMilkRate.objects.count(), 0)

    def test_invalid_milk_type_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_rate(milk_type="GOAT")

    def test_negative_rate_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_rate(rate=Decimal("-1.00"))

    def test_naive_effective_from_is_rejected(self):
        naive_datetime = datetime(2026, 10, 13, 10, 0, 0)

        with self.assertRaises(ValidationError):
            self.create_rate(effective_from=naive_datetime)

    def test_effective_to_must_be_after_effective_from(self):
        with self.assertRaises(ValidationError):
            self.create_rate(
                effective_to=self.start - timedelta(minutes=1),
            )

    def test_inactive_supplier_is_rejected(self):
        self.supplier.is_active = False
        self.supplier.save(update_fields=["is_active"])

        with self.assertRaises(ValidationError):
            self.create_rate()

    def test_cow_and_buffalo_rates_can_overlap(self):
        cow_rate = self.create_rate()

        buffalo_rate = self.create_rate(
            milk_type=SupplierMilkRate.MilkType.BUFFALO,
            rate=Decimal("18.00"),
        )

        self.assertNotEqual(cow_rate.pk, buffalo_rate.pk)
        self.assertEqual(SupplierMilkRate.objects.count(), 2)

    def test_adjacent_periods_are_allowed(self):
        end = self.start + timedelta(hours=1)

        old_rate = self.create_rate(effective_to=end)
        new_rate = self.create_rate(
            rate=Decimal("16.00"),
            effective_from=end,
        )

        self.assertLess(old_rate.effective_from, new_rate.effective_from)
        self.assertEqual(old_rate.effective_to, new_rate.effective_from)

    @skipUnless(
        connection.vendor == "postgresql",
        "PostgreSQL exclusion constraint test",
    )
    def test_overlapping_periods_for_same_supplier_and_type_are_rejected(self):
        end = self.start + timedelta(hours=2)

        self.create_rate(effective_to=end)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_rate(
                    rate=Decimal("16.00"),
                    effective_from=self.start + timedelta(hours=1),
                )

    def test_calculate_milk_amount_example(self):
        amount = calculate_milk_amount(
            litres="10",
            fat="9",
            rate="15",
        )
        self.assertEqual(amount, Decimal("1350.00"))


    def test_calculate_milk_amount_rounds_half_up(self):
        amount = calculate_milk_amount(
            litres="1",
            fat="1",
            rate="1.005",
        )
        self.assertEqual(amount, Decimal("1.01"))


    def test_negative_litres_are_rejected(self):
        with self.assertRaises(ValidationError):
            calculate_milk_amount(
                litres="-1",
                fat="9",
                rate="15",
            )


    def test_negative_fat_is_rejected(self):
        with self.assertRaises(ValidationError):
            calculate_milk_amount(
                litres="10",
                fat="-1",
                rate="15",
            )


    def test_rate_is_resolved_for_collection_timestamp(self):
        old_start = self.start - timedelta(hours=2)
        change_time = self.start
        collection_time = change_time + timedelta(minutes=1)

        self.create_rate(
            effective_from=old_start,
            effective_to=change_time,
            rate=Decimal("15.00"),
        )
        new_rate = self.create_rate(
            effective_from=change_time,
            rate=Decimal("20.00"),
        )

        resolved = resolve_supplier_milk_rate(
            business_id=self.business.id,
            supplier_id=self.supplier.id,
            milk_type=SupplierMilkRate.MilkType.COW,
            collected_at=collection_time,
        )

        self.assertEqual(resolved.id, new_rate.id)


    def test_exact_rate_change_time_uses_new_rate(self):
        change_time = self.start

        self.create_rate(
            effective_from=change_time - timedelta(hours=1),
            effective_to=change_time,
            rate=Decimal("15.00"),
        )
        new_rate = self.create_rate(
            effective_from=change_time,
            rate=Decimal("20.00"),
        )

        resolved = resolve_supplier_milk_rate(
            business_id=self.business.id,
            supplier_id=self.supplier.id,
            milk_type=SupplierMilkRate.MilkType.COW,
            collected_at=change_time,
        )

        self.assertEqual(resolved.id, new_rate.id)


    def test_missing_rate_is_rejected(self):
        with self.assertRaises(ValidationError):
            resolve_supplier_milk_rate(
                business_id=self.business.id,
                supplier_id=self.supplier.id,
                milk_type=SupplierMilkRate.MilkType.COW,
                collected_at=self.start,
            )


    def test_supplier_milk_amount_returns_rate_snapshot_values(self):
        self.create_rate(rate=Decimal("15.00"))

        result = calculate_supplier_milk_amount(
            business_id=self.business.id,
            supplier_id=self.supplier.id,
            milk_type=SupplierMilkRate.MilkType.COW,
            collected_at=self.start,
            litres="10",
            fat="9",
        )

        self.assertEqual(result["applied_rate"], Decimal("15.00"))
        self.assertEqual(result["amount"], Decimal("1350.00"))