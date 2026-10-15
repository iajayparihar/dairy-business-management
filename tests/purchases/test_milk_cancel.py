import threading
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from apps.businesses.models import Business, BusinessMembership
from apps.inventory.models import StockMovement
from apps.inventory.services import set_milk_config
from apps.payments.models import SupplierPaymentAllocation
from apps.payments.selectors import get_supplier_balance
from apps.payments.services import (
    allocate_supplier_advance,
    record_supplier_payment,
    reverse_supplier_payment,
)
from apps.products.models import Product
from apps.purchases.admin import MilkCollectionAdmin
from apps.purchases.models import MilkCollection, SupplierPurchase
from apps.purchases.services import (
    cancel_milk_collection,
    correct_milk_collection,
    create_milk_collection,
)
from apps.suppliers.models import Supplier, SupplierMilkRate

User = get_user_model()
COW = MilkCollection.MilkType.COW
BUFFALO = MilkCollection.MilkType.BUFFALO
IST = ZoneInfo("Asia/Kolkata")
WHEN = datetime(2026, 10, 15, 7, 0, tzinfo=IST)


class CancelFixture:
    def build(self, mapped=True):
        self.owner = User.objects.create_user(
            username="c_owner", password="test-password",
        )
        self.staff = User.objects.create_user(
            username="c_staff", password="test-password",
        )
        self.business = Business.objects.create(name="C", slug="c-dairy")
        for user, role in (
            (self.owner, BusinessMembership.Role.OWNER),
            (self.staff, BusinessMembership.Role.STAFF),
        ):
            BusinessMembership.objects.create(
                business=self.business, user=user, role=role, is_active=True,
            )
        self.supplier = Supplier.objects.create(
            business=self.business, supplier_code="RAM01", name="Ram",
            is_active=True,
        )
        self.rates = {}
        for milk_type, rate in ((COW, "38"), (BUFFALO, "15")):
            self.rates[milk_type] = SupplierMilkRate.objects.create(
                business=self.business, supplier=self.supplier,
                milk_type=milk_type, rate=Decimal(rate),
                effective_from=datetime(2026, 10, 1, tzinfo=IST),
                created_by=self.owner,
            )
        self.cow_p = Product.objects.create(
            business=self.business, sku="COWM", name="Cow",
            unit=Product.Unit.LITRE, track_stock=True,
        )
        self.buf_p = Product.objects.create(
            business=self.business, sku="BUFM", name="Buf",
            unit=Product.Unit.LITRE, track_stock=True,
        )
        if mapped:
            set_milk_config(
                business_id=self.business.id,
                cow_product_id=self.cow_p.id,
                buffalo_product_id=self.buf_p.id, user=self.owner,
            )

    def collect(self, milk_type=BUFFALO, litres="10", **kw):
        data = {
            "business_id": self.business.id,
            "supplier_id": self.supplier.id,
            "milk_type": milk_type,
            "collected_at": WHEN,
            "litres": litres,
            "fat": "9" if milk_type == BUFFALO else None,
            "user": self.owner,
        }
        data.update(kw)
        return create_milk_collection(**data)

    def cancel(self, collection, reason="wrong entry", user=None):
        return cancel_milk_collection(
            business_id=self.business.id, collection_id=collection.id,
            user=user or self.owner, reason=reason,
        )

    def correct(self, collection, litres, reason="re-weighed", **kw):
        return correct_milk_collection(
            business_id=self.business.id, collection_id=collection.id,
            user=self.owner, reason=reason, litres=litres, **kw,
        )

    def stock(self, product):
        return StockMovement.objects.filter(product=product).aggregate(
            t=Sum("quantity"),
        )["t"]

    def net_stock(self, product):
        from apps.inventory.services import stock_balance
        return stock_balance(
            business_id=self.business.id, product_id=product.id,
        )

    def balance(self):
        return get_supplier_balance(
            business_id=self.business.id, supplier_id=self.supplier.id,
        )

    def pay(self, amount, number="P1", allocations=None):
        return record_supplier_payment(
            business_id=self.business.id, supplier_id=self.supplier.id,
            payment_number=number, paid_at=WHEN, amount=Decimal(amount),
            user=self.owner, allocations=allocations,
        )


class CancelTests(CancelFixture, TestCase):
    def setUp(self):
        self.build()

    def test_cancel_keeps_row_cancels_purchase_and_reverses_stock(self):
        c = self.collect(litres="12.500")
        self.cancel(c, "wrong can")
        c.refresh_from_db()
        c.purchase.refresh_from_db()
        self.assertEqual(c.status, "CANCELLED")
        self.assertEqual(c.cancel_reason, "wrong can")
        self.assertEqual(c.cancelled_by, self.owner)
        self.assertIsNotNone(c.cancelled_at)
        self.assertEqual(c.cancel_stock_shortfall, Decimal("0"))
        self.assertEqual(c.purchase.status, SupplierPurchase.Status.CANCELLED)
        receipt = StockMovement.objects.get(
            milk_collection=c, movement_type="PURCHASE_RECEIPT",
        )
        reversal = StockMovement.objects.get(
            milk_collection=c, movement_type="REVERSAL_OUT",
        )
        self.assertEqual(reversal.reversal_of, receipt)
        self.assertEqual(reversal.quantity, receipt.quantity)
        self.assertEqual(reversal.occurred_at, receipt.occurred_at)
        self.assertEqual(self.net_stock(self.buf_p), Decimal("0"))
        self.assertEqual(MilkCollection.all_objects.count(), 1)

    def test_cancelled_entry_leaves_supplier_balance(self):
        keep = self.collect()
        gone = self.collect()
        self.assertEqual(self.balance()["total_purchases"], Decimal("2700.00"))
        self.cancel(gone)
        self.assertEqual(self.balance()["total_purchases"], Decimal("1350.00"))
        self.assertEqual(keep.amount, Decimal("1350.00"))

    def test_label_is_not_reused_after_cancel(self):
        first = self.collect()
        self.cancel(first)
        second = self.collect()
        self.assertEqual(first.collection_number, "RAM01-261015-M-B1")
        self.assertEqual(second.collection_number, "RAM01-261015-M-B2")

    def test_reason_is_required(self):
        c = self.collect()
        for reason in ("", "  ", "ab", None):
            with self.assertRaises(ValidationError):
                self.cancel(c, reason)
        c.refresh_from_db()
        self.assertEqual(c.status, "RECORDED")

    def test_double_cancel_is_rejected_and_reverses_once(self):
        c = self.collect()
        self.cancel(c)
        with self.assertRaises(ValidationError):
            self.cancel(c)
        self.assertEqual(
            StockMovement.objects.filter(
                milk_collection=c, movement_type="REVERSAL_OUT",
            ).count(), 1,
        )

    def test_staff_cannot_cancel(self):
        c = self.collect()
        with self.assertRaises(PermissionDenied):
            self.cancel(c, user=self.staff)
        c.refresh_from_db()
        self.assertEqual(c.status, "RECORDED")

    def test_unknown_or_other_business_entry_does_not_exist(self):
        c = self.collect()
        other = Business.objects.create(name="X", slug="x-dairy")
        BusinessMembership.objects.create(
            business=other, user=self.owner,
            role=BusinessMembership.Role.OWNER, is_active=True,
        )
        with self.assertRaises(MilkCollection.DoesNotExist):
            cancel_milk_collection(
                business_id=other.id, collection_id=c.id,
                user=self.owner, reason="nope",
            )

    def test_consumed_milk_is_cancelled_and_shortfall_flagged(self):
        c = self.collect(litres="20")
        StockMovement.objects.create(
            business=self.business, product=self.buf_p,
            movement_type="DELIVERY", quantity=Decimal("15"),
            occurred_at=WHEN,
        )
        self.cancel(c)
        c.refresh_from_db()
        self.assertEqual(c.status, "CANCELLED")
        self.assertEqual(c.cancel_stock_shortfall, Decimal("15.000"))
        self.assertEqual(self.net_stock(self.buf_p), Decimal("-15.000"))

    def test_no_shortfall_when_enough_stock_remains(self):
        c = self.collect(litres="20")
        self.collect(litres="30")
        StockMovement.objects.create(
            business=self.business, product=self.buf_p,
            movement_type="DELIVERY", quantity=Decimal("15"),
            occurred_at=WHEN,
        )
        self.cancel(c)
        c.refresh_from_db()
        self.assertEqual(c.cancel_stock_shortfall, Decimal("0"))

    def test_cancel_without_mapping_has_no_stock_effect(self):
        MilkCollection.objects.all().delete()
        from apps.inventory.models import BusinessMilkConfig
        BusinessMilkConfig.objects.all().delete()
        c = self.collect()
        self.cancel(c)
        self.assertFalse(StockMovement.objects.exists())

    def test_reversal_goes_to_original_product_even_if_mapping_changed(self):
        c = self.collect()
        set_milk_config(
            business_id=self.business.id, cow_product_id=self.cow_p.id,
            buffalo_product_id=None, user=self.owner,
        )
        self.cancel(c)
        self.assertEqual(self.net_stock(self.buf_p), Decimal("0"))

    def test_cancelling_a_paid_entry_returns_money_to_credit(self):
        # Payment first (advance); the new entry auto-uses it.
        self.pay("1000", "ADV")
        c = self.collect()  # 1350: 1000 auto-allocated
        before = self.balance()
        self.assertEqual(before["allocated_payments"], Decimal("1000.00"))
        self.cancel(c)  # allowed even though money is allocated
        after = self.balance()
        self.assertEqual(after["total_purchases"], Decimal("0.00"))
        self.assertEqual(after["allocated_payments"], Decimal("0.00"))
        self.assertEqual(after["outstanding_amount"], Decimal("0.00"))
        self.assertEqual(after["available_credit"], Decimal("1000.00"))
        # The freed advance is usable again by the next entry.
        again = self.collect()
        self.assertEqual(
            SupplierPaymentAllocation.objects.filter(
                purchase=again.purchase,
            ).aggregate(t=Sum("amount"))["t"], Decimal("1000.00"),
        )

    def test_cancelling_after_the_payment_was_reversed_also_works(self):
        payment = self.pay("500", "ADV")
        c = self.collect()
        reverse_supplier_payment(
            payment_id=payment.id, reason="bounced", user=self.owner,
        )
        self.cancel(c)
        self.assertEqual(self.balance()["outstanding_amount"], Decimal("0.00"))

    def test_admin_is_read_only(self):
        admin = MilkCollectionAdmin(MilkCollection, AdminSite())
        c = self.collect()
        self.assertFalse(admin.has_delete_permission(None, c))
        self.assertFalse(admin.has_add_permission(None))
        self.assertIn("status", admin.get_readonly_fields(None, c))
        self.assertIn("amount", admin.get_readonly_fields(None, c))


class CorrectTests(CancelFixture, TestCase):
    def setUp(self):
        self.build()

    def test_correct_cancels_old_and_replaces_at_original_time_and_shift(self):
        old = self.collect(litres="10", collected_at=WHEN, shift="EVENING")
        new = self.correct(old, litres="8")
        old.refresh_from_db()
        self.assertEqual(old.status, "CANCELLED")
        self.assertEqual(old.cancel_reason, "Corrected: re-weighed")
        self.assertEqual(new.replaces_id, old.id)
        self.assertEqual(new.status, "RECORDED")
        self.assertEqual(new.collected_at, WHEN)
        self.assertEqual(new.shift, "EVENING")
        self.assertEqual(new.litres, Decimal("8"))
        self.assertEqual(new.fat, Decimal("9"))       # kept
        self.assertEqual(new.amount, Decimal("1080.00"))
        self.assertEqual(new.collection_number, "RAM01-261015-E-B2")
        self.assertEqual(self.net_stock(self.buf_p), Decimal("8.000"))
        self.assertEqual(self.balance()["total_purchases"], Decimal("1080.00"))

    def test_rate_is_the_one_in_force_at_the_original_time(self):
        old = self.collect()
        rate = self.rates[BUFFALO]
        rate.effective_to = WHEN + timedelta(hours=1)
        rate.save(update_fields=["effective_to"])
        SupplierMilkRate.objects.create(
            business=self.business, supplier=self.supplier,
            milk_type=BUFFALO, rate=Decimal("99"),
            effective_from=WHEN + timedelta(hours=1), created_by=self.owner,
        )
        new = self.correct(old, litres="10", fat="8")
        self.assertEqual(new.applied_rate, Decimal("15.00"))
        self.assertEqual(new.amount, Decimal("1200.00"))

    def test_correction_shortfall_is_judged_after_the_replacement(self):
        old = self.collect(litres="20")
        StockMovement.objects.create(
            business=self.business, product=self.buf_p,
            movement_type="DELIVERY", quantity=Decimal("15"),
            occurred_at=WHEN,
        )
        self.correct(old, litres="18")      # 3 L left: no shortfall
        old.refresh_from_db()
        self.assertEqual(old.cancel_stock_shortfall, Decimal("0"))
        newer = MilkCollection.objects.get(status="RECORDED")
        self.correct(newer, litres="10")    # only 10 L vs 15 used: -5
        newer.refresh_from_db()
        self.assertEqual(newer.cancel_stock_shortfall, Decimal("5.000"))
        self.assertEqual(self.net_stock(self.buf_p), Decimal("-5.000"))

    def test_cow_correction_is_flat_and_rejects_fat(self):
        old = self.collect(COW, "10")
        with self.assertRaises(ValidationError):
            self.correct(old, litres="9", fat="5")
        old.refresh_from_db()
        self.assertEqual(old.status, "RECORDED")
        new = self.correct(old, litres="9")
        self.assertEqual(new.amount, Decimal("342.00"))
        self.assertEqual(new.fat, Decimal("0"))

    def test_nothing_to_correct(self):
        old = self.collect()
        with self.assertRaises(ValidationError):
            self.correct(old, litres="10")
        with self.assertRaises(ValidationError):
            self.correct(old, litres="10", fat="9")

    def test_cannot_correct_a_cancelled_entry(self):
        old = self.collect()
        self.cancel(old)
        with self.assertRaises(ValidationError):
            self.correct(old, litres="5")

    def test_failure_rolls_everything_back(self):
        old = self.collect()
        count = MilkCollection.all_objects.count()
        with mock.patch(
            "apps.purchases.services.milk_product_for",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.correct(old, litres="5")
        old.refresh_from_db()
        old.purchase.refresh_from_db()
        self.assertEqual(old.status, "RECORDED")
        self.assertEqual(old.purchase.status, "RECEIVED")
        self.assertEqual(MilkCollection.all_objects.count(), count)
        self.assertEqual(self.net_stock(self.buf_p), Decimal("10.000"))

    def test_correcting_a_paid_entry_moves_the_money_to_the_replacement(self):
        self.pay("1000", "ADV")
        old = self.collect()
        new = self.correct(old, litres="5")   # 675
        self.assertEqual(
            SupplierPaymentAllocation.objects.filter(
                purchase=new.purchase,
            ).aggregate(t=Sum("amount"))["t"], Decimal("675.00"),
        )
        bal = self.balance()
        self.assertEqual(bal["outstanding_amount"], Decimal("0.00"))
        self.assertEqual(bal["available_credit"], Decimal("325.00"))

    def test_staff_cannot_correct(self):
        old = self.collect()
        with self.assertRaises(PermissionDenied):
            correct_milk_collection(
                business_id=self.business.id, collection_id=old.id,
                user=self.staff, reason="x" * 5, litres="5",
            )


class CancelAPITests(CancelFixture, TestCase):
    def setUp(self):
        self.build()
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.base = (
            f"/api/v1/businesses/{self.business.id}/suppliers/"
            f"{self.supplier.id}/milk-collections/"
        )

    def test_cancel_endpoint(self):
        c = self.collect()
        r = self.client.post(
            f"{self.base}{c.id}/cancel/", {"reason": "wrong can"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "CANCELLED")
        self.assertEqual(r.data["cancel_reason"], "wrong can")
        again = self.client.post(
            f"{self.base}{c.id}/cancel/", {"reason": "wrong can"},
            format="json",
        )
        self.assertEqual(again.status_code, 400)

    def test_cancel_requires_reason(self):
        c = self.collect()
        for body in ({}, {"reason": ""}, {"reason": "ab"}):
            r = self.client.post(
                f"{self.base}{c.id}/cancel/", body, format="json",
            )
            self.assertEqual(r.status_code, 400, body)

    def test_correct_endpoint_returns_new_old_and_shortfall(self):
        c = self.collect(litres="20")
        StockMovement.objects.create(
            business=self.business, product=self.buf_p,
            movement_type="DELIVERY", quantity=Decimal("15"),
            occurred_at=WHEN,
        )
        r = self.client.post(
            f"{self.base}{c.id}/correct/",
            {"reason": "re-weighed", "litres": "18"}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["collection"]["litres"], "18.000")
        self.assertEqual(r.data["collection"]["replaces"], str(c.id))
        self.assertEqual(r.data["cancelled_collection"]["status"], "CANCELLED")
        self.assertEqual(Decimal(str(r.data["stock_shortfall"])), Decimal("0"))

    def test_correct_cow_with_fat_is_400(self):
        c = self.collect(COW, "10")
        r = self.client.post(
            f"{self.base}{c.id}/correct/",
            {"reason": "re-weighed", "litres": "9", "fat": "5"},
            format="json",
        )
        self.assertEqual(r.status_code, 400)

    def test_staff_forbidden_and_wrong_supplier_404(self):
        c = self.collect()
        self.client.force_authenticate(self.staff)
        r = self.client.post(
            f"{self.base}{c.id}/cancel/", {"reason": "abc"}, format="json",
        )
        self.assertEqual(r.status_code, 403)
        self.client.force_authenticate(self.owner)
        other = Supplier.objects.create(
            business=self.business, supplier_code="SHY02", name="S",
            is_active=True,
        )
        url = (
            f"/api/v1/businesses/{self.business.id}/suppliers/{other.id}/"
            f"milk-collections/{c.id}/cancel/"
        )
        self.assertEqual(
            self.client.post(url, {"reason": "abc"}, format="json").status_code,
            404,
        )

    def test_detail_shows_cancel_fields(self):
        c = self.collect()
        self.cancel(c)
        r = self.client.get(f"{self.base}{c.id}/")
        self.assertEqual(r.data["status"], "CANCELLED")
        self.assertIn("cancel_stock_shortfall", r.data)


class CancelConcurrencyTests(CancelFixture, TransactionTestCase):
    def setUp(self):
        self.build()

    def run_threads(self, calls):
        errors, results = [], []
        barrier = threading.Barrier(len(calls))

        def worker(call):
            try:
                barrier.wait(timeout=10)
                results.append(call())
            except Exception as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(c,)) for c in calls]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
            self.assertFalse(t.is_alive(), "deadlock: thread did not finish")
        return results, errors

    def test_double_cancel_race_reverses_stock_once(self):
        c = self.collect()
        results, errors = self.run_threads([lambda: self.cancel(c)] * 2)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValidationError)
        self.assertEqual(
            StockMovement.objects.filter(
                milk_collection=c, movement_type="REVERSAL_OUT",
            ).count(), 1,
        )
        self.assertEqual(self.net_stock(self.buf_p), Decimal("0"))

    def test_cancel_vs_allocation_never_corrupts_the_books(self):
        for n in range(4):
            c = self.collect(litres="10", collected_at=WHEN + timedelta(
                minutes=n))
            payment = self.pay("500", f"RACE-{n}")  # unallocated advance
            # collect() auto-allocated earlier advances; use a fresh pair.
            results, errors = self.run_threads([
                lambda: self.cancel(c),
                lambda: allocate_supplier_advance(
                    payment_id=payment.id, purchase_id=c.purchase_id,
                    amount=Decimal("100"), user=self.owner,
                ),
            ])
            for exc in errors:
                # The only acceptable losers: allocation after cancel, or
                # an advance that was already used by the auto-allocation.
                self.assertIsInstance(exc, ValidationError, repr(exc))
            c.refresh_from_db()
            c.purchase.refresh_from_db()
            self.assertEqual(c.status, "CANCELLED")
            self.assertEqual(c.purchase.status, "CANCELLED")

        bal = self.balance()
        # Everything cancelled: nothing owed, nothing "allocated", and the
        # whole of every payment is available again.
        self.assertEqual(bal["total_purchases"], Decimal("0.00"))
        self.assertEqual(bal["allocated_payments"], Decimal("0.00"))
        self.assertEqual(bal["outstanding_amount"], Decimal("0.00"))
        self.assertEqual(bal["available_credit"], Decimal("2000.00"))

    def test_correct_vs_cancel_race_leaves_one_live_or_none(self):
        old = self.collect(litres="10")
        results, errors = self.run_threads([
            lambda: self.cancel(old),
            lambda: self.correct(old, litres="5"),
        ])
        for exc in errors:
            self.assertIsInstance(exc, ValidationError, repr(exc))
        live = MilkCollection.objects.filter(status="RECORDED").count()
        live_stock = self.net_stock(self.buf_p)
        if live == 0:   # cancel won (or correct lost)
            self.assertEqual(live_stock, Decimal("0"))
        else:           # correct won, replacement is the only live row
            self.assertEqual(live, 1)
            self.assertEqual(live_stock, Decimal("5.000"))
