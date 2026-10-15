import threading
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from apps.businesses.models import Business, BusinessMembership
from apps.purchases.models import MilkCollection, SupplierPurchase
from apps.purchases.services import (
    ClientRefConflict,
    create_milk_collection,
)
from apps.suppliers.models import Supplier, SupplierMilkRate

User = get_user_model()
COW = MilkCollection.MilkType.COW
BUFFALO = MilkCollection.MilkType.BUFFALO
IST = ZoneInfo("Asia/Kolkata")


def ist(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=IST)


class IdentityFixture:
    """Shared setup. Rates start well before the dates used below."""

    def build(self):
        self.owner = User.objects.create_user(
            username="ident_owner", password="test-password",
        )
        self.staff = User.objects.create_user(
            username="ident_staff", password="test-password",
        )
        self.business = Business.objects.create(
            name="Ident Dairy", slug="ident-dairy",
        )
        for user, role in (
            (self.owner, BusinessMembership.Role.OWNER),
            (self.staff, BusinessMembership.Role.STAFF),
        ):
            BusinessMembership.objects.create(
                business=self.business, user=user, role=role,
                is_active=True,
            )
        self.supplier = Supplier.objects.create(
            business=self.business, supplier_code="RAM01",
            name="Ram", is_active=True,
        )
        self.other = Supplier.objects.create(
            business=self.business, supplier_code="SHY02",
            name="Shyam", is_active=True,
        )
        for supplier in (self.supplier, self.other):
            for milk_type, rate in ((COW, "38"), (BUFFALO, "15")):
                SupplierMilkRate.objects.create(
                    business=self.business, supplier=supplier,
                    milk_type=milk_type, rate=Decimal(rate),
                    effective_from=ist(1, 0), created_by=self.owner,
                )

    def create(self, **overrides):
        data = {
            "business_id": self.business.id,
            "supplier_id": self.supplier.id,
            "milk_type": BUFFALO,
            "collected_at": ist(15, 7),
            "litres": "10",
            "fat": "9",
            "user": self.owner,
        }
        data.update(overrides)
        return create_milk_collection(**data)


class MilkLabelAndShiftTests(IdentityFixture, TestCase):
    def setUp(self):
        self.build()

    def test_label_format_and_sequence_per_can(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first.collection_number, "RAM01-261015-M-B1")
        self.assertEqual(second.collection_number, "RAM01-261015-M-B2")
        self.assertEqual(
            second.purchase.purchase_number, "MILK-RAM01-261015-M-B2",
        )

    def test_sequence_is_per_supplier_date_shift_and_type(self):
        self.create()
        cow = self.create(milk_type=COW, fat=None)
        evening = self.create(collected_at=ist(15, 17))
        next_day = self.create(collected_at=ist(16, 7))
        other = self.create(supplier_id=self.other.id)
        self.assertEqual(cow.collection_number, "RAM01-261015-M-C1")
        self.assertEqual(evening.collection_number, "RAM01-261015-E-B1")
        self.assertEqual(next_day.collection_number, "RAM01-261016-M-B1")
        self.assertEqual(other.collection_number, "SHY02-261015-M-B1")

    def test_label_date_uses_business_local_day(self):
        # 00:30 IST on the 16th is still the 15th in UTC.
        c = self.create(collected_at=ist(16, 0, 30))
        self.assertEqual(c.collection_number, "RAM01-261016-M-B1")

    def test_number_is_not_reused_after_a_row_is_removed(self):
        first = self.create()
        MilkCollection.all_objects.filter(pk=first.pk).update(
            is_deleted=True,
        )
        second = self.create()
        self.assertEqual(second.collection_number, "RAM01-261015-M-B2")

    def test_shift_defaults_from_collected_at_local_time(self):
        self.assertEqual(
            self.create(collected_at=ist(15, 14, 59)).shift, "MORNING",
        )
        self.assertEqual(
            self.create(collected_at=ist(15, 15, 0)).shift, "EVENING",
        )

    def test_explicit_shift_wins_and_is_in_the_label(self):
        c = self.create(collected_at=ist(15, 7), shift="EVENING")
        self.assertEqual(c.shift, "EVENING")
        self.assertEqual(c.collection_number, "RAM01-261015-E-B1")

    def test_invalid_shift_rejected(self):
        with self.assertRaises(ValidationError):
            self.create(shift="NOON")

    def test_staff_cannot_create(self):
        with self.assertRaises(Exception):
            self.create(user=self.staff)


class MilkClientRefTests(IdentityFixture, TestCase):
    def setUp(self):
        self.build()

    def test_same_request_returns_original_without_new_rows(self):
        first = self.create(client_ref="r1")
        again = self.create(client_ref="r1")
        self.assertEqual(first.pk, again.pk)
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)
        self.assertEqual(MilkCollection.objects.count(), 1)
        self.assertEqual(SupplierPurchase.objects.count(), 1)

    def test_omitted_shift_is_resolved_before_comparing(self):
        first = self.create(client_ref="r1")  # 07:00 -> MORNING
        again = self.create(client_ref="r1", shift="MORNING")
        self.assertEqual(first.pk, again.pk)

    def test_any_difference_is_a_conflict(self):
        self.create(client_ref="r1")
        for change in (
            {"litres": "11"},
            {"fat": "8"},
            {"shift": "EVENING"},
            {"collected_at": ist(15, 8)},
            {"milk_type": COW, "fat": None},
            {"supplier_id": self.other.id},
        ):
            with self.assertRaises(ClientRefConflict, msg=str(change)):
                self.create(client_ref="r1", **change)
        self.assertEqual(MilkCollection.objects.count(), 1)

    def test_same_ref_is_allowed_in_another_business(self):
        self.create(client_ref="r1")
        other = Business.objects.create(name="B2", slug="b2")
        BusinessMembership.objects.create(
            business=other, user=self.owner,
            role=BusinessMembership.Role.OWNER, is_active=True,
        )
        supplier = Supplier.objects.create(
            business=other, supplier_code="X1", name="X", is_active=True,
        )
        SupplierMilkRate.objects.create(
            business=other, supplier=supplier, milk_type=BUFFALO,
            rate=Decimal("15"), effective_from=ist(1, 0),
            created_by=self.owner,
        )
        c = create_milk_collection(
            business_id=other.id, supplier_id=supplier.id,
            milk_type=BUFFALO, collected_at=ist(15, 7), litres="10",
            fat="9", client_ref="r1", user=self.owner,
        )
        self.assertFalse(c.replayed)


class MilkIdentityAPITests(IdentityFixture, TestCase):
    def setUp(self):
        self.build()
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.url = (
            f"/api/v1/businesses/{self.business.id}/suppliers/"
            f"{self.supplier.id}/milk-collections/"
        )

    def post(self, **body):
        payload = {
            "milk_type": "BUFFALO", "litres": "10", "fat": "9",
            "collected_at": ist(15, 7).isoformat(),
        }
        payload.update(body)
        return self.client.post(self.url, payload, format="json")

    def test_label_and_shift_in_response(self):
        r = self.post()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["collection_number"], "RAM01-261015-M-B1")
        self.assertEqual(r.data["shift"], "MORNING")

    def test_client_cannot_send_a_label(self):
        r = self.post(collection_number="MINE-1")
        self.assertEqual(r.status_code, 400)
        self.assertIn("collection_number", r.data)
        self.assertEqual(MilkCollection.objects.count(), 0)

    def test_replay_is_200_and_conflict_is_409(self):
        self.assertEqual(self.post(client_ref="a").status_code, 201)
        again = self.post(client_ref="a")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data["collection_number"], "RAM01-261015-M-B1")
        self.assertEqual(self.post(client_ref="a", litres="11").status_code, 409)
        self.assertEqual(MilkCollection.objects.count(), 1)

    def test_invalid_shift_is_400(self):
        self.assertEqual(self.post(shift="NOON").status_code, 400)


class MilkConcurrentCreateTests(IdentityFixture, TransactionTestCase):
    def setUp(self):
        self.build()

    def run_threads(self, calls):
        errors, results = [], []

        def worker(call):
            try:
                results.append(call())
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(c,)) for c in calls]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results, errors

    def test_parallel_creates_get_unique_labels(self):
        results, errors = self.run_threads([self.create] * 6)
        self.assertEqual(errors, [])
        labels = sorted(c.collection_number for c in results)
        self.assertEqual(
            labels, [f"RAM01-261015-M-B{n}" for n in range(1, 7)],
        )
        self.assertEqual(MilkCollection.objects.count(), 6)

    def test_parallel_retries_with_same_ref_create_one_row(self):
        results, errors = self.run_threads(
            [lambda: self.create(client_ref="same")] * 5
        )
        self.assertEqual(errors, [])
        self.assertEqual(len({c.pk for c in results}), 1)
        self.assertEqual(MilkCollection.objects.count(), 1)
        self.assertEqual(SupplierPurchase.objects.count(), 1)
