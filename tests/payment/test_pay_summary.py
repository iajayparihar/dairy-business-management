"""Phase 4 Step 3: read-only pay-for-period helper."""
from datetime import datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from tests.payment.helpers import DecimalClient

from apps.businesses.models import BusinessMembership
from apps.payments.models import SupplierPayment, SupplierPaymentAllocation
from apps.payments.selectors import get_supplier_balance
from apps.purchases.models import SupplierPurchase
from apps.suppliers.models import Supplier
from tests.payment.test_payment_hardening import IST, Base, User
from tests.payment.test_payment_reversals import ReversalMixin

D = Decimal


class PaySummaryTests(ReversalMixin, TestCase):
    def setUp(self):
        self.build()
        self.staff = User.objects.create_user("stf", password="pw-123456")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff, role="STAFF", is_active=True
        )
        self.client = DecimalClient()
        self.client.force_authenticate(self.owner)
        self.url = (
            f"/api/v1/businesses/{self.business.id}/suppliers/"
            f"{self.supplier.id}/pay-summary/"
        )

    def get(self, query="", **kw):
        return self.client.get(self.url + query, **kw)

    def ago(self, days):
        return timezone.now() - timedelta(days=days)

    def test_default_five_days_splits_period_from_older(self):
        self.purchase("OLD", "300.00", self.ago(10))
        self.purchase("NEW1", "500.00", self.ago(2))
        self.purchase("NEW2", "200.00", self.ago(0.1))
        r = self.get()

        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["period"]["days"], 5)
        self.assertEqual(D(r.data["outstanding_amount"]), D("1000.00"))
        self.assertEqual(r.data["suggested_amount"], D("1000.00"))
        self.assertEqual(
            [p["purchase_number"] for p in r.data["period_purchases"]],
            ["NEW1", "NEW2"],
        )
        b = r.data["breakdown"]
        self.assertEqual(b["period_outstanding"], D("700.00"))
        self.assertEqual(b["outstanding_outside_period"], D("300.00"))

    def test_existing_payments_and_allocations_reduce_due(self):
        self.purchase("OLD", "300.00", self.ago(10))
        self.purchase("NEW", "500.00", self.ago(1))
        self.pay(amount="400.00", paid_at=self.ago(0.5), auto_allocate=True)  # OLD 300 + NEW 100
        r = self.get()

        row = r.data["period_purchases"][0]
        self.assertEqual((D(row["paid"]), D(row["outstanding"])), (D("100.00"), D("400.00")))
        self.assertEqual(r.data["outstanding_amount"], D("400.00"))
        pay = r.data["period_payments"][0]
        self.assertEqual((pay["amount"], pay["allocated"], pay["unallocated"]),
                         (D("400.00"), D("400.00"), D("0.00")))

    def test_advance_is_netted_in_suggestion_not_in_outstanding(self):
        self.pay(amount="400.00", paid_at=self.ago(3))  # advance, nothing to apply to
        self.purchase("P", "1000.00", self.ago(1))
        r = self.get()
        self.assertEqual(D(r.data["outstanding_amount"]), D("1000.00"))
        self.assertEqual(r.data["available_credit"], D("400.00"))
        self.assertEqual(r.data["suggested_amount"], D("600.00"))

    def test_refund_due_never_suggests_negative(self):
        p = self.purchase("P", "100.00", self.ago(1))
        self.pay(amount="100.00", auto_allocate=True)
        SupplierPurchase.objects.filter(pk=p.pk).update(total_amount=D("60.00"))
        r = self.get()
        self.assertEqual(D(r.data["outstanding_amount"]), D("-40.00"))
        self.assertEqual(r.data["suggested_amount"], D("0.00"))
        self.assertEqual(r.data["refund_due"], D("40.00"))

    def test_reversal_and_cancelled_purchase_are_respected(self):
        self.purchase("A", "500.00", self.ago(1))
        gone = self.purchase("GONE", "700.00", self.ago(1))
        pay = self.pay(amount="1200.00", auto_allocate=True)
        self.reverse(pay, "500.00")
        SupplierPurchase.objects.filter(pk=gone.pk).update(
            status=SupplierPurchase.Status.CANCELLED
        )
        r = self.get()
        self.assertEqual(
            [p["purchase_number"] for p in r.data["period_purchases"]], ["A"]
        )
        bal = get_supplier_balance(
            business_id=self.business.id, supplier_id=self.supplier.id
        )
        self.assertEqual(D(r.data["outstanding_amount"]), bal["outstanding_amount"])
        self.assertEqual(r.data["available_credit"], bal["available_credit"])
        b = r.data["breakdown"]
        self.assertEqual(
            b["period_outstanding"] + b["outstanding_outside_period"],
            r.data["outstanding_amount"],
        )

    def test_custom_range_is_half_open_in_business_timezone(self):
        # 23:59:59 IST on the 14th is outside; 00:00 on the 15th is inside;
        # 23:59:59 on the 16th is inside; 00:00 on the 17th is outside.
        for name, at in (
            ("BEFORE", datetime(2026, 10, 14, 23, 59, 59, tzinfo=IST)),
            ("FIRST", datetime(2026, 10, 15, 0, 0, 0, tzinfo=IST)),
            ("LAST", datetime(2026, 10, 16, 23, 59, 59, tzinfo=IST)),
            ("AFTER", datetime(2026, 10, 17, 0, 0, 0, tzinfo=IST)),
        ):
            self.purchase(name, "10.00", at)
        r = self.get("?start_date=2026-10-15&end_date=2026-10-16")
        self.assertEqual(
            [p["purchase_number"] for p in r.data["period_purchases"]],
            ["FIRST", "LAST"],
        )
        self.assertEqual(D(r.data["outstanding_amount"]), D("40.00"))
        self.assertEqual(r.data["breakdown"]["outstanding_outside_period"], D("20.00"))

    def test_invalid_parameters_are_400(self):
        for q in ("?days=0", "?days=367", "?days=abc", "?days=2&start_date=2026-10-01&end_date=2026-10-02",
                  "?start_date=2026-10-01", "?start_date=x&end_date=2026-10-02",
                  "?start_date=2026-10-05&end_date=2026-10-01",
                  "?start_date=2024-01-01&end_date=2026-01-01"):
            self.assertEqual(self.get(q).status_code, 400, q)

    def test_read_only_and_access_control(self):
        self.purchase("P", "100.00", self.ago(1))
        before = (SupplierPayment.objects.count(), SupplierPaymentAllocation.objects.count())
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(
            before,
            (SupplierPayment.objects.count(), SupplierPaymentAllocation.objects.count()),
        )

        staff = DecimalClient()
        staff.force_authenticate(self.staff)
        self.assertEqual(staff.get(self.url).status_code, 403)

        outsider = DecimalClient()
        outsider.force_authenticate(self.other_owner)
        self.assertEqual(outsider.get(self.url).status_code, 403)

        # Own business URL, another business's supplier: 404.
        foreign = Supplier.objects.create(business=self.other, supplier_code="F", name="F")
        other_client = DecimalClient()
        other_client.force_authenticate(self.other_owner)
        url = (f"/api/v1/businesses/{self.business.id}/suppliers/{foreign.id}/pay-summary/")
        self.assertEqual(other_client.get(url).status_code, 403)
        own = DecimalClient()
        own.force_authenticate(self.owner)
        self.assertEqual(own.get(url).status_code, 404)

    def test_empty_supplier(self):
        r = self.get()
        self.assertEqual(D(r.data["suggested_amount"]), D("0.00"))
        self.assertEqual(r.data["period_purchases"], [])
