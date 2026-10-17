"""Balance, unbilled deliveries and the account statement."""
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.billing.payment_services import reverse_customer_payment
from apps.billing.statements import (
    get_customer_account_statement,
    get_customer_balance,
)
from apps.deliveries.services import cancel_delivery
from tests.billing.base import BillFlow, ist

D = Decimal


class BalanceTests(BillFlow, TestCase):
    def balance(self):
        return get_customer_balance(
            business_id=self.business.pk, customer_id=self.customer.pk,
        )

    def test_new_customer_is_at_zero(self):
        balance = self.balance()
        self.assertEqual(balance["net_balance"], D("0.00"))
        self.assertEqual(balance["unbilled_amount"], D("0.00"))
        self.assertEqual(balance["unbilled_deliveries"], 0)

    def test_unbilled_deliveries_are_reported_but_not_in_the_balance(self):
        total = self.september()

        before = self.balance()
        self.assertEqual(before["net_balance"], D("0.00"))
        self.assertEqual(before["unbilled_amount"], total)
        self.assertEqual(before["unbilled_deliveries"], 5)

        self.finalize(self.draft())
        after = self.balance()
        self.assertEqual(after["net_balance"], total)
        self.assertEqual(after["unbilled_amount"], D("0.00"))
        self.assertEqual(after["unbilled_deliveries"], 0)

    def test_deliveries_after_the_bill_stay_unbilled(self):
        self.september()
        self.finalize(self.draft())
        self.deliver(self.cow, "1", ist(2026, 10, 2))

        balance = self.balance()

        self.assertEqual(balance["unbilled_amount"], D("60.00"))
        self.assertEqual(balance["unbilled_deliveries"], 1)

    def test_voiding_makes_the_deliveries_unbilled_again(self):
        total = self.september()
        self.void(self.finalize(self.draft()))

        balance = self.balance()

        self.assertEqual(balance["net_balance"], D("0.00"))
        self.assertEqual(balance["unbilled_amount"], total)

    def test_cancelled_deliveries_are_not_unbilled(self):
        gone = self.deliver(self.cow, "1", ist(2026, 9, 2))
        cancel_delivery(
            business_id=self.business.pk, delivery_id=gone.pk,
            user=self.owner, reason="mistake",
        )
        self.assertEqual(self.balance()["unbilled_deliveries"], 0)

    def test_several_unpaid_bills_then_a_payment_clears_the_oldest(self):
        self.deliver(self.cow, "5", ist(2026, 8, 10))     # 300
        self.finalize(self.draft(period=(date(2026, 8, 1), date(2026, 8, 31))))
        self.deliver(self.cow, "10", ist(2026, 9, 10))    # 600
        sep = self.finalize(self.draft())
        self.assertEqual(self.balance()["outstanding_amount"], D("900.00"))

        self.pay("300")

        balance = self.balance()
        self.assertEqual(balance["outstanding_amount"], D("600.00"))
        self.assertEqual(self.paid_on(sep), D("0"))


class StatementTests(BillFlow, TestCase):
    def statement(self, start=None, end=None):
        return get_customer_account_statement(
            business_id=self.business.pk, customer_id=self.customer.pk,
            start_date=start, end_date=end,
        )

    def entries(self, statement):
        return [(t["type"], t["balance"]) for t in statement["transactions"]]

    def test_bill_payment_and_reversal_with_a_running_balance(self):
        self.deliver(self.cow, "15", ist(2026, 8, 10))        # 900
        self.finalize(self.draft(period=(date(2026, 8, 1), date(2026, 8, 31))))
        payment = self.pay("1000")
        reverse_customer_payment(
            business_id=self.business.pk, payment_id=payment.pk, amount="200",
            reason="bounced", user=self.owner,
        )

        statement = self.statement()

        self.assertEqual(self.entries(statement), [
            ("BILL", D("900.00")),
            ("PAYMENT", D("-100.00")),
            ("PAYMENT_REVERSAL", D("100.00")),
        ])
        self.assertEqual(statement["closing_balance"], D("100.00"))
        self.assertEqual(statement["customer_name"], "Ravi")
        position = self.assert_reconciled()
        self.assertEqual(statement["closing_balance"], position["net_balance"])
        self.assertEqual(
            statement["closing_balance"],
            statement["outstanding_amount"] - statement["available_credit"],
        )

    def test_opening_balance_carries_what_happened_before_the_start(self):
        self.deliver(self.cow, "5", ist(2026, 8, 10))         # 300
        self.finalize(self.draft(period=(date(2026, 8, 1), date(2026, 8, 31))))
        today = timezone.now().date()
        self.pay("100")

        statement = self.statement(start=today + timedelta(days=1))

        self.assertEqual(statement["opening_balance"], D("200.00"))
        self.assertEqual(statement["transactions"], [])
        self.assertEqual(statement["closing_balance"], D("200.00"))

    def test_an_old_period_reads_the_same_after_a_later_void(self):
        t0 = timezone.now() - timedelta(days=10)
        bill = self.make_bill(status="FINALIZED", total="300", finalized_at=t0)
        bill.status = "VOID"
        bill.voided_at = t0 + timedelta(days=6)
        bill.void_reason = "wrong delivery"
        bill.save(update_fields=["status", "voided_at", "void_reason", "updated_at"])

        old = self.statement(end=(t0 + timedelta(days=3)).date())
        self.assertEqual(self.entries(old), [("BILL", D("300.00"))])
        self.assertEqual(old["closing_balance"], D("300.00"))
        self.assertEqual(old["outstanding_amount"], D("300.00"))

        full = self.statement()
        self.assertEqual(
            self.entries(full), [("BILL", D("300.00")), ("BILL_VOID", D("0.00"))],
        )
        self.assertEqual(full["closing_balance"], D("0.00"))

    def test_voided_draft_never_appears(self):
        self.september()
        self.void(self.draft(), reason="discard it")
        self.assertEqual(self.statement()["transactions"], [])

    def test_date_validation(self):
        with self.assertRaises(ValidationError):
            self.statement(start="not-a-date")
        with self.assertRaises(ValidationError):
            self.statement(start="2026-09-30", end="2026-09-01")
