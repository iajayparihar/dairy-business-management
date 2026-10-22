"""Phase 4 Step 2: partial reversal, advance allocation, and their APIs."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient
from tests.payment.helpers import DecimalClient

from apps.businesses.models import BusinessMembership
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentAllocationReversal,
    SupplierPaymentReversal,
)
from apps.payments.selectors import get_payment_amounts, get_supplier_balance
from apps.payments.services import (
    ClientRefConflict,
    allocate_supplier_advance,
    reverse_supplier_payment,
)
from apps.payments.statements import get_supplier_account_statement
from apps.purchases.models import SupplierPurchase
from apps.purchases.services import create_milk_collection
from apps.suppliers.models import SupplierMilkRate
from tests.payment.test_payment_hardening import Base, User

D = Decimal


class ReversalMixin(Base):
    def reverse(self, payment, amount=None, reason="mistake", **kw):
        return reverse_supplier_payment(
            payment_id=payment.pk, amount=amount, reason=reason,
            user=self.owner, **kw,
        )

    def alloc(self, payment, purchase, amount):
        return allocate_supplier_advance(
            payment_id=payment.pk, purchase_id=purchase.pk,
            amount=amount, user=self.owner,
        )

    def amounts(self, payment):
        return get_payment_amounts(SupplierPayment.objects.get(pk=payment.pk))

    def balance(self):
        return get_supplier_balance(
            business_id=self.business.id, supplier_id=self.supplier.id
        )

    def assert_reconciled(self):
        st = get_supplier_account_statement(
            business_id=self.business.id, supplier_id=self.supplier.id
        )
        bal = self.balance()
        for key in ("total_purchases", "allocated_payments",
                    "outstanding_amount", "available_credit"):
            self.assertEqual(st[key], bal[key], key)
        self.assertEqual(
            st["closing_balance"],
            st["outstanding_amount"] - st["available_credit"],
        )


class PartialReversalTests(ReversalMixin, TestCase):
    def setUp(self):
        self.build()
        self.p1 = self.purchase("P1", "400.00", timezone.now() - timedelta(days=2))
        self.p2 = self.purchase("P2", "600.00", timezone.now() - timedelta(days=1))

    def test_full_reversal_unwinds_every_allocation_explicitly(self):
        pay = self.pay(amount="1000.00", auto_allocate=True)
        rev = self.reverse(pay)

        self.assertEqual(rev.amount, D("1000.00"))
        self.assertEqual(len(rev.unwound), 2)
        self.assertEqual(self.amounts(pay), {
            "amount": D("1000.00"), "reversed": D("1000.00"),
            "net": D("0.00"), "allocated": D("0.00"), "unallocated": D("0.00"),
        })
        # History intact: rows still there, nothing edited.
        self.assertEqual(pay.allocations.count(), 2)
        self.assertEqual(self.balance()["outstanding_amount"], D("1000.00"))
        self.assertEqual(self.balance()["available_credit"], D("0.00"))
        self.assert_reconciled()

    def test_partial_reversal_consumes_advance_first(self):
        pay = self.pay(amount="1300.00", auto_allocate=True)  # 300 advance
        rev = self.reverse(pay, "200.00")

        self.assertEqual(rev.unwound, [])
        self.assertEqual(SupplierPaymentAllocationReversal.objects.count(), 0)
        a = self.amounts(pay)
        self.assertEqual((a["net"], a["allocated"], a["unallocated"]),
                         (D("1100.00"), D("1000.00"), D("100.00")))
        self.assertEqual(self.balance()["outstanding_amount"], D("0.00"))
        self.assertEqual(self.balance()["available_credit"], D("100.00"))
        self.assert_reconciled()

    def test_partial_reversal_unwinds_newest_allocation_first(self):
        pay = self.pay(amount="1000.00")  # all advance
        self.alloc(pay, self.p1, "400.00")
        self.alloc(pay, self.p2, "600.00")

        rev = self.reverse(pay, "700.00")  # P2 600 first, then P1 100

        got = [(u.allocation.purchase.purchase_number, u.amount) for u in rev.unwound]
        self.assertEqual(got, [("P2", D("600.00")), ("P1", D("100.00"))])
        self.assertEqual(self.balance()["outstanding_amount"], D("700.00"))
        a = self.amounts(pay)
        self.assertEqual((a["net"], a["allocated"], a["unallocated"]),
                         (D("300.00"), D("300.00"), D("0.00")))
        self.assert_reconciled()

    def test_mixed_advance_then_allocations(self):
        pay = self.pay(amount="1000.00")
        self.alloc(pay, self.p1, "400.00")  # 600 advance left
        rev = self.reverse(pay, "700.00")   # 600 advance + 100 from P1

        self.assertEqual(rev.amount - sum(u.amount for u in rev.unwound), D("600.00"))
        self.assertEqual(self.balance()["outstanding_amount"], D("1000.00") - D("300.00"))
        self.assert_reconciled()

    def test_cumulative_reversals_cannot_exceed_payment(self):
        pay = self.pay(amount="1000.00", auto_allocate=True)
        self.reverse(pay, "600.00")
        with self.assertRaises(ValidationError):
            self.reverse(pay, "500.00")
        self.reverse(pay, "400.00")
        with self.assertRaises(ValidationError):
            self.reverse(pay, "0.01")
        with self.assertRaises(ValidationError):
            self.reverse(pay)  # nothing left
        self.assertEqual(
            SupplierPaymentReversal.objects.filter(payment=pay).aggregate(
                t=Sum("amount"))["t"], D("1000.00"))
        # No allocation is ever unwound below zero.
        for alloc in pay.allocations.all():
            unwound = alloc.unwinds.aggregate(t=Sum("amount"))["t"]
            self.assertLessEqual(unwound, alloc.amount)

    def test_invalid_amounts_and_reason(self):
        pay = self.pay(amount="100.00")
        for bad in ("0", "-5", "abc", "1.005"):
            with self.assertRaises(ValidationError):
                self.reverse(pay, bad)
        with self.assertRaises(ValidationError):
            self.reverse(pay, "10.00", reason="   ")
        self.assertEqual(SupplierPaymentReversal.objects.count(), 0)

    def test_amount_omitted_reverses_what_is_left(self):
        pay = self.pay(amount="1000.00", auto_allocate=True)
        self.reverse(pay, "250.00")
        rev = self.reverse(pay)
        self.assertEqual(rev.amount, D("750.00"))
        self.assertEqual(self.amounts(pay)["net"], D("0.00"))

    def test_advance_allocation_respects_net_after_partial_reversal(self):
        pay = self.pay(amount="1000.00")
        self.reverse(pay, "300.00")  # net 700
        with self.assertRaises(ValidationError):
            self.alloc(pay, self.p2, "800.00")
        self.alloc(pay, self.p2, "600.00")
        with self.assertRaises(ValidationError):
            self.alloc(pay, self.p1, "200.00")  # only 100 left
        self.alloc(pay, self.p1, "100.00")
        self.assertEqual(self.amounts(pay)["unallocated"], D("0.00"))

    def test_fully_reversed_payment_cannot_be_allocated(self):
        pay = self.pay(amount="100.00")
        self.reverse(pay)
        with self.assertRaises(ValidationError):
            self.alloc(pay, self.p1, "10.00")

    def test_allocation_on_cancelled_purchase_is_ignored_not_unwound(self):
        pay = self.pay(amount="1000.00")
        self.alloc(pay, self.p1, "400.00")
        self.alloc(pay, self.p2, "600.00")
        SupplierPurchase.objects.filter(pk=self.p2.pk).update(
            status=SupplierPurchase.Status.CANCELLED
        )  # 600 returns to advance
        self.assertEqual(self.amounts(pay)["unallocated"], D("600.00"))

        rev = self.reverse(pay, "500.00")  # all from advance
        self.assertEqual(rev.unwound, [])
        self.assertEqual(self.amounts(pay)["unallocated"], D("100.00"))
        self.assert_reconciled()

    def test_duplicate_allocation_same_amount_replays_different_rejected(self):
        pay = self.pay(amount="1000.00")
        first = self.alloc(pay, self.p1, "300.00")
        again = self.alloc(pay, self.p1, "300.00")
        self.assertTrue(again.replayed)
        self.assertEqual(first.pk, again.pk)
        with self.assertRaises(ValidationError):
            self.alloc(pay, self.p1, "100.00")
        self.assertEqual(pay.allocations.count(), 1)

    def test_unwound_allocation_is_not_a_replay(self):
        pay = self.pay(amount="1000.00")
        self.alloc(pay, self.p1, "300.00")
        self.reverse(pay, "1000.00")
        with self.assertRaises(ValidationError):
            self.alloc(pay, self.p1, "300.00")

    def test_client_ref_replay_and_conflict(self):
        pay = self.pay(amount="1000.00", auto_allocate=True)
        first = self.reverse(pay, "300.00", client_ref="rv1")
        again = self.reverse(pay, "300.00", client_ref="rv1")
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(SupplierPaymentReversal.objects.count(), 1)
        with self.assertRaises(ClientRefConflict):
            self.reverse(pay, "301.00", client_ref="rv1")
        with self.assertRaises(ClientRefConflict):
            self.reverse(pay, "300.00", reason="other", client_ref="rv1")

    def test_statement_shows_each_reversal_and_is_time_consistent(self):
        pay = self.pay(amount="1000.00", auto_allocate=True)
        self.reverse(pay, "100.00")
        self.reverse(pay, "200.00")
        st = get_supplier_account_statement(
            business_id=self.business.id, supplier_id=self.supplier.id
        )
        types = [t["type"] for t in st["transactions"]]
        self.assertEqual(types.count("PAYMENT_REVERSAL"), 2)
        self.assertEqual(st["total_payments"], D("700.00"))
        self.assert_reconciled()

        # As of a day before the reversals nothing is reversed yet.
        yesterday = (timezone.now() - timedelta(days=1)).date()
        past = get_supplier_account_statement(
            business_id=self.business.id, supplier_id=self.supplier.id,
            end_date=yesterday,
        )
        self.assertEqual(past["total_payments"], D("0.00"))  # paid today

    def test_ledger_rows_are_immutable(self):
        pay = self.pay(amount="1000.00", auto_allocate=True)
        rev = self.reverse(pay, "1000.00")
        unwind = SupplierPaymentAllocationReversal.objects.first()
        with self.assertRaises(ValueError):
            unwind.delete()
        with self.assertRaises(ValueError):
            SupplierPaymentAllocationReversal.objects.all().update(amount=D("1"))
        with self.assertRaises(ValueError):
            rev.delete()

    def test_milk_auto_apply_uses_net_advance(self):
        pay = self.pay(amount="500.00")
        self.reverse(pay, "300.00")  # net 200
        now = timezone.now()
        SupplierMilkRate.objects.create(
            business=self.business, supplier=self.supplier,
            milk_type=SupplierMilkRate.MilkType.COW, rate=D("10.00"),
            effective_from=now - timedelta(days=30), created_by=self.owner,
        )
        create_milk_collection(
            business_id=self.business.id, supplier_id=self.supplier.id,
            milk_type="COW", collected_at=now, litres=D("100.000"),
            user=self.owner,
        )  # costs 1000, only 200 advance is real
        self.assertEqual(self.amounts(pay)["allocated"], D("200.00"))
        self.assertEqual(self.amounts(pay)["unallocated"], D("0.00"))


class ReversalApiTests(ReversalMixin, TestCase):
    def setUp(self):
        self.build()
        self.p1 = self.purchase("P1", "400.00", timezone.now() - timedelta(days=2))
        self.p2 = self.purchase("P2", "600.00", timezone.now() - timedelta(days=1))
        self.pay_obj = self.pay(amount="1000.00")
        self.manager = User.objects.create_user("mgr", password="pw-123456")
        self.staff = User.objects.create_user("stf", password="pw-123456")
        for user, role in ((self.manager, "MANAGER"), (self.staff, "STAFF")):
            BusinessMembership.objects.create(
                business=self.business, user=user, role=role, is_active=True
            )
        self.client = DecimalClient()
        self.client.force_authenticate(self.owner)
        self.base = f"/api/v1/businesses/{self.business.id}"

    def url(self, action=""):
        return f"{self.base}/payments/{self.pay_obj.id}/{action}"

    def test_allocate_then_detail_then_reverse(self):
        r = self.client.post(self.url("allocate/"), {
            "purchase_id": str(self.p2.id), "amount": "600.00"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(r.data["payment"]["amounts"]["unallocated"]), D("400.00"))

        # Retry of the same allocation is a 200 replay, not a duplicate.
        r = self.client.post(self.url("allocate/"), {
            "purchase_id": str(self.p2.id), "amount": "600.00"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SupplierPaymentAllocation.objects.count(), 1)

        r = self.client.post(self.url("reverse/"), {
            "amount": "500.00", "reason": "wrong", "client_ref": "k1"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["reversal"]["from_advance"], D("400.00"))
        self.assertEqual(r.data["reversal"]["from_allocations"][0]["amount"], D("100.00"))

        r2 = self.client.post(self.url("reverse/"), {
            "amount": "500.00", "reason": "wrong", "client_ref": "k1"}, format="json")
        self.assertEqual(r2.status_code, 200)
        r3 = self.client.post(self.url("reverse/"), {
            "amount": "400.00", "reason": "wrong", "client_ref": "k1"}, format="json")
        self.assertEqual(r3.status_code, 409)

        d = self.client.get(self.url())
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.data["amounts"]["net"], D("500.00"))
        self.assertEqual(len(d.data["reversals"]), 1)
        self.assertEqual(d.data["allocations"][0]["effective"], D("500.00"))

    def test_excessive_and_invalid_requests_are_400(self):
        for body in ({"amount": "1000.01", "reason": "x1"},
                     {"amount": "10.00"},
                     {"amount": "0", "reason": "x1"}):
            r = self.client.post(self.url("reverse/"), body, format="json")
            self.assertEqual(r.status_code, 400, body)
        r = self.client.post(self.url("allocate/"), {
            "purchase_id": str(self.p1.id), "amount": "1000.00"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(self.url("allocate/"), {
            "purchase_id": str(uuid.uuid4()), "amount": "10.00"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(SupplierPaymentReversal.objects.count(), 0)

    def test_roles_and_tenant_isolation(self):
        body = {"amount": "10.00", "reason": "xx1"}
        outsider = DecimalClient()
        outsider.force_authenticate(self.other_owner)
        self.assertEqual(outsider.post(self.url("reverse/"), body, format="json").status_code, 403)
        self.assertEqual(outsider.get(self.url()).status_code, 403)

        # Own business URL with a foreign payment id: 404, no leak.
        other_client = DecimalClient()
        other_client.force_authenticate(self.other_owner)
        foreign = (f"/api/v1/businesses/{self.other.id}/payments/"
                   f"{self.pay_obj.id}/")
        self.assertEqual(other_client.get(foreign).status_code, 404)
        self.assertEqual(other_client.post(foreign + "reverse/", body, format="json").status_code, 404)

        staff = DecimalClient()
        staff.force_authenticate(self.staff)
        self.assertEqual(staff.post(self.url("reverse/"), body, format="json").status_code, 403)
        self.assertEqual(staff.get(self.url()).status_code, 403)

        mgr = DecimalClient()
        mgr.force_authenticate(self.manager)
        self.assertEqual(mgr.post(self.url("reverse/"), body, format="json").status_code, 201)
        self.assertEqual(SupplierPaymentReversal.objects.count(), 1)

    def test_balance_endpoint(self):
        url = f"{self.base}/suppliers/{self.supplier.id}/balance/"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(D(r.data["outstanding_amount"]), D("1000.00"))
        self.assertEqual(r.data["available_credit"], D("1000.00"))
        staff = DecimalClient()
        staff.force_authenticate(self.staff)
        self.assertEqual(staff.get(url).status_code, 403)


class ReversalConcurrencyTests(ReversalMixin, TransactionTestCase):
    def setUp(self):
        self.build()
        self.p1 = self.purchase("P1", "1000.00", timezone.now() - timedelta(days=1))

    def _race(self, fns):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        barrier = Barrier(len(fns))

        def run(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return fn()
            except ValidationError:
                return "rejected"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(fns)) as ex:
            return list(ex.map(run, fns))

    def test_two_reversals_cannot_exceed_payment(self):
        pay = self.pay(amount="1000.00", auto_allocate=True)
        res = self._race([
            lambda: self.reverse(pay, "700.00") and "ok",
            lambda: self.reverse(pay, "700.00") and "ok",
        ])
        self.assertEqual(sorted(res), ["ok", "rejected"])
        self.assertEqual(self.amounts(pay)["net"], D("300.00"))
        self.assertEqual(self.balance()["outstanding_amount"], D("700.00"))
        self.assert_reconciled()

    def test_reversals_that_fit_both_succeed_without_double_unwind(self):
        pay = self.pay(amount="1000.00", auto_allocate=True)
        res = self._race([
            lambda: self.reverse(pay, "400.00") and "ok",
            lambda: self.reverse(pay, "600.00") and "ok",
        ])
        self.assertEqual(res, ["ok", "ok"])
        alloc = pay.allocations.get()
        self.assertEqual(alloc.unwinds.aggregate(t=Sum("amount"))["t"], D("1000.00"))
        self.assertEqual(self.balance()["outstanding_amount"], D("1000.00"))

    def test_reverse_vs_allocate_never_overspend_the_payment(self):
        p2 = self.purchase("P2", "1000.00")
        pay = self.pay(amount="1000.00")
        res = self._race([
            lambda: self.reverse(pay, "700.00") and "ok",
            lambda: self.alloc(pay, p2, "500.00") and "ok",
        ])
        a = self.amounts(pay)
        self.assertGreaterEqual(a["unallocated"], D("0.00"))
        self.assertLessEqual(a["allocated"], a["net"])
        self.assertIn("ok", res)

    def test_duplicate_reverse_with_same_client_ref_creates_one(self):
        pay = self.pay(amount="1000.00", auto_allocate=True)
        res = self._race([
            lambda: "replay" if self.reverse(pay, "300.00", client_ref="z").replayed else "new",
            lambda: "replay" if self.reverse(pay, "300.00", client_ref="z").replayed else "new",
        ])
        self.assertEqual(sorted(res), ["new", "replay"])
        self.assertEqual(SupplierPaymentReversal.objects.count(), 1)

    def test_reverse_vs_cancel_purchase_keeps_invariants(self):
        pay = self.pay(amount="1000.00", auto_allocate=True)

        def cancel():
            SupplierPurchase.objects.filter(pk=self.p1.pk).update(
                status=SupplierPurchase.Status.CANCELLED)
            return "ok"

        self._race([lambda: self.reverse(pay, "400.00") and "ok", cancel])
        a = self.amounts(pay)
        self.assertEqual(a["net"], D("600.00"))
        self.assertGreaterEqual(a["unallocated"], D("0.00"))
        self.assertLessEqual(a["allocated"], a["net"])
