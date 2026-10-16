"""Phase 4 Step 1: payment idempotency, tenant scoping, one-clock statements."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Barrier
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.payments.models import SupplierPayment, SupplierPaymentAllocation
from apps.payments.selectors import get_supplier_balance
from apps.payments.services import (
    ClientRefConflict,
    allocate_supplier_advance,
    record_supplier_payment,
    reverse_supplier_payment,
)
from apps.payments.statements import get_supplier_account_statement
from apps.purchases.models import SupplierPurchase
from apps.suppliers.models import Supplier

User = get_user_model()
IST = ZoneInfo("Asia/Kolkata")


class Base:
    def build(self):
        self.owner = User.objects.create_user("own", password="pw-123456")
        self.other_owner = User.objects.create_user("oth", password="pw-123456")
        self.business = create_business(name="A", slug="a", owner=self.owner)
        self.other = create_business(name="B", slug="b", owner=self.other_owner)
        self.supplier = Supplier.objects.create(
            business=self.business, supplier_code="S1", name="S1"
        )

    def purchase(self, number="P1", amount="1000.00", at=None):
        at = at or timezone.now()
        return SupplierPurchase.objects.create(
            business=self.business, supplier=self.supplier,
            purchase_number=number, purchased_at=at,
            status=SupplierPurchase.Status.RECEIVED,
            total_amount=Decimal(amount), created_by=self.owner,
            received_at=at,
        )

    def pay(self, number="PAY1", amount="400.00", paid_at=None, **kw):
        return record_supplier_payment(
            business_id=self.business.id, supplier_id=self.supplier.id,
            payment_number=number, paid_at=paid_at or timezone.now(),
            amount=Decimal(amount), user=self.owner, **kw,
        )


class PaymentIdempotencyTests(Base, TestCase):
    def setUp(self):
        self.build()
        self.purchase()

    def test_same_client_ref_replays_without_new_rows(self):
        paid_at = timezone.now()
        first = self.pay(paid_at=paid_at, client_ref="r1", auto_allocate=True)
        second = self.pay(paid_at=paid_at, client_ref="r1", auto_allocate=True)

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SupplierPayment.objects.count(), 1)
        self.assertEqual(SupplierPaymentAllocation.objects.count(), 1)

    def test_same_client_ref_different_data_conflicts(self):
        paid_at = timezone.now()
        self.pay(paid_at=paid_at, client_ref="r1")
        with self.assertRaises(ClientRefConflict):
            self.pay(paid_at=paid_at, client_ref="r1", amount="401.00")
        with self.assertRaises(ClientRefConflict):
            self.pay(paid_at=paid_at, client_ref="r1", notes="changed")
        self.assertEqual(SupplierPayment.objects.count(), 1)

    def test_replay_compares_explicit_allocations(self):
        paid_at = timezone.now()
        p = SupplierPurchase.objects.get(purchase_number="P1")
        alloc = [{"purchase_id": p.pk, "amount": "100.00"}]
        self.pay(paid_at=paid_at, client_ref="r1", allocations=alloc)
        self.pay(paid_at=paid_at, client_ref="r1", allocations=alloc)
        with self.assertRaises(ClientRefConflict):
            self.pay(paid_at=paid_at, client_ref="r1", allocations=[])

    def test_duplicate_number_without_ref_is_validation_error(self):
        self.pay()
        with self.assertRaises(ValidationError):
            self.pay(amount="10.00")

    def test_auto_and_explicit_allocations_are_exclusive(self):
        with self.assertRaises(ValidationError):
            self.pay(auto_allocate=True, allocations=[{"purchase_id": "x", "amount": "1"}])

    def test_api_replay_200_and_conflict_409(self):
        client = APIClient()
        client.force_authenticate(self.owner)
        url = (
            f"/api/v1/businesses/{self.business.id}/suppliers/"
            f"{self.supplier.id}/payments/"
        )
        body = {
            "payment_number": "API1", "paid_at": "2026-10-15T10:00:00+05:30",
            "amount": "250.00", "client_ref": "api-1",
        }
        self.assertEqual(client.post(url, body, format="json").status_code, 201)
        self.assertEqual(client.post(url, body, format="json").status_code, 200)
        body["amount"] = "251.00"
        self.assertEqual(client.post(url, body, format="json").status_code, 409)
        self.assertEqual(SupplierPayment.objects.count(), 1)


class TenantScopingTests(Base, TestCase):
    def setUp(self):
        self.build()
        self.p = self.purchase()
        self.payment = self.pay()

    def test_unknown_and_foreign_payment_give_validation_error(self):
        import uuid
        for pid in (uuid.uuid4(), "not-a-uuid"):
            with self.assertRaises(ValidationError):
                reverse_supplier_payment(
                    payment_id=pid, reason="x", user=self.owner,
                    business_id=self.business.id,
                )
        # Payment belongs to A; scoped to B it is simply "not found".
        with self.assertRaises(ValidationError):
            reverse_supplier_payment(
                payment_id=self.payment.pk, reason="x",
                user=self.other_owner, business_id=self.other.id,
            )
        with self.assertRaises(ValidationError):
            allocate_supplier_advance(
                payment_id=self.payment.pk, purchase_id=self.p.pk,
                amount="10.00", user=self.other_owner,
                business_id=self.other.id,
            )

    def test_unknown_purchase_is_validation_error(self):
        import uuid
        with self.assertRaises(ValidationError):
            allocate_supplier_advance(
                payment_id=self.payment.pk, purchase_id=uuid.uuid4(),
                amount="10.00", user=self.owner,
            )


class OneClockStatementTests(Base, TestCase):
    def setUp(self):
        self.build()

    def test_backdated_auto_allocated_payment_shows_in_old_statement(self):
        """Old bug: allocations were filtered by created_at (today)."""
        old = timezone.now() - timedelta(days=10)
        self.purchase(at=old - timedelta(hours=1))
        self.pay(paid_at=old, auto_allocate=True, amount="1000.00")

        st = get_supplier_account_statement(
            business_id=self.business.id, supplier_id=self.supplier.id,
            end_date=old.astimezone(IST).date(),
        )
        self.assertEqual(st["allocated_payments"], Decimal("1000.00"))
        self.assertEqual(st["outstanding_amount"], Decimal("0.00"))

    def test_day_boundary_uses_business_timezone(self):
        # 00:30 IST on 16 Oct == 19:00 UTC on 15 Oct.
        at = datetime(2026, 10, 16, 0, 30, tzinfo=IST)
        self.purchase(at=at - timedelta(days=3))
        self.pay(paid_at=at, amount="100.00")

        def entries(day):
            return get_supplier_account_statement(
                business_id=self.business.id, supplier_id=self.supplier.id,
                start_date=day, end_date=day,
            )["transactions"]

        self.assertEqual(len(entries("2026-10-16")), 1)
        self.assertEqual(len(entries("2026-10-15")), 0)

    def test_end_of_day_is_exclusive_next_midnight(self):
        at = datetime(2026, 10, 15, 23, 59, 59, tzinfo=IST)
        nxt = datetime(2026, 10, 16, 0, 0, 0, tzinfo=IST)
        self.purchase(at=at - timedelta(days=3))
        self.pay(number="A", paid_at=at, amount="10.00")
        self.pay(number="B", paid_at=nxt, amount="20.00")
        st = get_supplier_account_statement(
            business_id=self.business.id, supplier_id=self.supplier.id,
            start_date="2026-10-15", end_date="2026-10-15",
        )
        self.assertEqual(st["total_payments"], Decimal("10.00"))

    def test_statement_reconciles_with_balance(self):
        self.purchase(number="P1", amount="1000.00")
        self.purchase(number="P2", amount="500.00")
        self.pay(number="A", amount="1200.00", auto_allocate=True)
        self.pay(number="B", amount="900.00", auto_allocate=True)  # 200 advance
        reverse_supplier_payment(
            payment_id=SupplierPayment.objects.get(payment_number="A").pk,
            reason="bounced", user=self.owner,
        )
        st = get_supplier_account_statement(
            business_id=self.business.id, supplier_id=self.supplier.id,
        )
        bal = get_supplier_balance(
            business_id=self.business.id, supplier_id=self.supplier.id
        )
        for key in ("total_purchases", "allocated_payments",
                    "outstanding_amount", "available_credit"):
            self.assertEqual(st[key], bal[key], key)
        self.assertEqual(
            st["closing_balance"],
            st["outstanding_amount"] - st["available_credit"],
        )

    def test_refund_due_is_not_clamped(self):
        p = self.purchase(amount="100.00")
        self.pay(amount="100.00", auto_allocate=True)
        SupplierPurchase.objects.filter(pk=p.pk).update(
            total_amount=Decimal("60.00")
        )
        bal = get_supplier_balance(
            business_id=self.business.id, supplier_id=self.supplier.id
        )
        self.assertEqual(bal["outstanding_amount"], Decimal("-40.00"))


class PaymentConcurrencyHardeningTests(Base, TransactionTestCase):
    def setUp(self):
        self.build()
        self.purchase(amount="1000.00")

    def _run(self, fn, args):
        barrier = Barrier(len(args))

        def worker(arg):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return fn(arg)
            except ValidationError as exc:
                return type(exc).__name__
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(args)) as ex:
            return list(ex.map(worker, args))

    def test_same_client_ref_race_creates_one_payment(self):
        paid_at = timezone.now()

        def go(_):
            p = self.pay(paid_at=paid_at, client_ref="race", auto_allocate=True)
            return "replay" if p.replayed else "new"

        results = self._run(go, [1, 2])
        self.assertEqual(sorted(results), ["new", "replay"])
        self.assertEqual(SupplierPayment.objects.count(), 1)

    def test_concurrent_auto_allocations_never_overpay(self):
        def go(n):
            self.pay(number=f"C{n}", amount="700.00", auto_allocate=True)
            return "ok"

        self._run(go, [1, 2])
        allocated = SupplierPaymentAllocation.objects.aggregate(
            t=Sum("amount")
        )["t"]
        self.assertEqual(allocated, Decimal("1000.00"))
        self.assertEqual(SupplierPayment.objects.count(), 2)  # 400 stays advance
