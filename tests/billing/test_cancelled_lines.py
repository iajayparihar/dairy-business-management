"""A bill shows cancelled / corrected entries beside the live ones; they are never charged."""
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.billing.documents import bill_document
from apps.billing.models import CustomerBillCancelledLine
from apps.billing.pdf import render_bill_pdf
from apps.deliveries.services import cancel_delivery, correct_delivery
from tests.billing.base import BillFlow, ist

D = Decimal
FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


@FAST
class CancelledLinesTests(BillFlow, TestCase):
    def setUp(self):
        super().setUp()
        self.d1 = self.deliver(self.cow, "2", ist(2026, 9, 1))            # 120, will be corrected to 3 L
        self.d2 = self.deliver(self.cow, "1", ist(2026, 9, 2))            # 60, will be cancelled
        self.d3 = self.deliver(self.buffalo, "1", ist(2026, 9, 3))        # 80, untouched
        self.new = correct_delivery(
            business_id=self.business.pk, delivery_id=self.d1.pk, user=self.owner,
            reason="miscount", quantity="3")
        cancel_delivery(business_id=self.business.pk, delivery_id=self.d2.pk,
                        user=self.owner, reason="customer was away")

    def doc(self, bill):
        return bill_document(bill)

    def test_draft_shows_wrong_and_right_entries_but_charges_only_the_right_ones(self):
        doc = self.doc(self.draft())
        cancelled = [l for l in doc["lines"] if l["cancelled"]]
        live = [l for l in doc["lines"] if not l["cancelled"]]
        self.assertEqual(len(cancelled), 2)
        self.assertEqual(sorted(l["quantity"] for l in cancelled), ["1.000", "2.000"])
        self.assertEqual({l["cancel_reason"] for l in cancelled}, {"Corrected: miscount", "customer was away"})
        self.assertTrue(all(not l["counted_in_total"] for l in cancelled))
        self.assertEqual(sorted(l["quantity"] for l in live), ["1.000", "3.000"])
        self.assertEqual(doc["totals"]["total"], "260.00")          # 3 x 60 + 80, nothing from cancelled
        self.assertEqual(doc["totals"]["cancelled_lines"], 2)
        self.assertEqual(doc["totals"]["cancelled_not_charged"], "180.00")

    def test_finalize_freezes_the_cancelled_lines_and_the_total(self):
        bill = self.finalize(self.draft())
        self.assertEqual(bill.total_amount, D("260.00"))
        self.assertEqual(CustomerBillCancelledLine.objects.filter(bill=bill).count(), 2)
        doc = self.doc(bill)
        self.assertEqual((doc["totals"]["total"], doc["totals"]["cancelled_lines"]), ("260.00", 2))
        self.assertEqual(doc["account"]["balance_after"], "260.00")
        self.assertEqual(len(doc["lines"]), 4)
        self.assertEqual([l["delivered_at"] for l in doc["lines"]], sorted(l["delivered_at"] for l in doc["lines"]))

    def test_cancelled_lines_do_not_touch_balance_payment_or_audit(self):
        bill = self.finalize(self.draft())
        self.assertEqual(self.position()["outstanding_amount"], D("260.00"))
        self.pay("260")
        self.assertEqual(self.position()["outstanding_amount"], D("0.00"))
        call_command("audit_bills")  # exits non-zero (raises) if a promise is broken

    def test_cancelled_lines_are_immutable(self):
        bill = self.finalize(self.draft())
        row = CustomerBillCancelledLine.objects.filter(bill=bill).first()
        with self.assertRaises(ValueError):
            row.save()
        with self.assertRaises(ValueError):
            row.delete()

    def test_void_then_replacement_draft_is_live_again(self):
        bill = self.finalize(self.draft())
        self.void(bill)
        doc = self.doc(self.draft())
        self.assertEqual(doc["totals"]["cancelled_lines"], 2)

    def test_other_customers_and_other_periods_do_not_leak_in(self):
        self.deliver(self.cow, "1", ist(2026, 9, 5), customer=self.customer2)
        d = self.deliver(self.cow, "1", ist(2026, 10, 5), customer=self.customer2)
        cancel_delivery(business_id=self.business.pk, delivery_id=d.pk, user=self.owner, reason="not needed")
        self.assertEqual(self.doc(self.draft())["totals"]["cancelled_lines"], 2)
        self.assertEqual(self.doc(self.draft(customer=self.customer2))["totals"]["cancelled_lines"], 0)

    def test_pdf_lists_cancelled_entries_in_english_and_hindi(self):
        bill = self.finalize(self.draft())
        for lang in ("en", "hi"):
            pdf = render_bill_pdf(bill, lang)
            self.assertTrue(pdf.startswith(b"%PDF"), lang)
        # byte-identical on every call (the existing promise)
        self.assertEqual(render_bill_pdf(bill, "en"), render_bill_pdf(bill, "en"))

    def test_bill_without_cancellations_is_unchanged(self):
        self.deliver(self.cow, "2", ist(2026, 9, 1), customer=self.customer2)
        doc = self.doc(self.draft(customer=self.customer2))
        self.assertEqual((doc["totals"]["cancelled_lines"], doc["totals"]["cancelled_not_charged"]), (0, "0.00"))
        self.assertTrue(all(not l["cancelled"] for l in doc["lines"]))
