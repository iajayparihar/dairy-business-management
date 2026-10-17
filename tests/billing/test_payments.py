"""Customer payments: recording, allocation, reversal and the balance maths."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import (
    CustomerBill,
    CustomerPayment,
    CustomerPaymentAllocation,
    CustomerPaymentAllocationReversal,
    CustomerPaymentReversal,
)
from apps.billing.payment_services import (
    allocate_customer_advance,
    record_customer_payment,
    reverse_customer_payment,
)
from apps.billing.selectors import get_payment_amounts
from apps.customers.models import Customer
from apps.payments.services import ClientRefConflict
from tests.billing.base import BillingBase

D = Decimal


class PaymentFixtures(BillingBase):
    """Bills are inserted directly: these tests are about the payment side."""

    def setUp(self):
        self.build()

    def bill(self, start, end, total, customer=None, status="FINALIZED"):
        return self.make_bill(
            customer=customer, status=status, start=start, end=end, total=total,
        )

    def aug(self, total="600"):
        return self.bill(date(2026, 8, 1), date(2026, 8, 31), total)

    def sep(self, total="900"):
        return self.bill(date(2026, 9, 1), date(2026, 9, 30), total)

    def alloc(self, bill, amount):
        return {"bill_id": str(bill.pk), "amount": str(amount)}

    def amounts(self, payment):
        return get_payment_amounts(CustomerPayment.objects.get(pk=payment.pk))

    def paid_on(self, bill):
        return sum(
            (a.amount for a in CustomerPaymentAllocation.objects.filter(bill=bill)),
            D("0"),
        )


class RecordPaymentTests(PaymentFixtures, TestCase):
    def test_pays_the_oldest_bill_first(self):
        aug, sep = self.aug("600"), self.sep("900")

        payment = self.pay("700")

        rows = {row.bill_id: row.amount for row in payment.allocation_rows}
        self.assertEqual(rows, {aug.pk: D("600.00"), sep.pk: D("100.00")})
        self.assertFalse(payment.replayed)
        position = self.assert_reconciled()
        self.assertEqual(position["total_billed"], D("1500.00"))
        self.assertEqual(position["outstanding_amount"], D("800.00"))
        self.assertEqual(position["available_credit"], D("0.00"))
        self.assertEqual(position["net_balance"], D("800.00"))

    def test_partial_then_full_payment(self):
        sep = self.sep("900")

        self.pay("300")
        self.assertEqual(self.assert_reconciled()["outstanding_amount"], D("600.00"))

        self.pay("600")
        position = self.assert_reconciled()
        self.assertEqual(position["outstanding_amount"], D("0.00"))
        self.assertEqual(position["net_balance"], D("0.00"))
        self.assertEqual(self.paid_on(sep), D("900.00"))

    def test_paying_more_than_the_bill_leaves_an_advance(self):
        # The client's own example: 1000 paid against a 900 bill.
        sep = self.sep("900")

        payment = self.pay("1000")

        self.assertEqual(self.paid_on(sep), D("900.00"))
        self.assertEqual(self.amounts(payment)["unallocated"], D("100.00"))
        position = self.assert_reconciled()
        self.assertEqual(position["available_credit"], D("100.00"))
        self.assertEqual(position["net_balance"], D("-100.00"))

    def test_payment_before_any_bill_exists_is_all_advance(self):
        payment = self.pay("1000")

        self.assertEqual(payment.allocation_rows, [])
        position = self.assert_reconciled()
        self.assertEqual(position["available_credit"], D("1000.00"))
        self.assertEqual(position["net_balance"], D("-1000.00"))

    def test_drafts_and_void_bills_are_never_paid(self):
        draft = self.bill(date(2026, 8, 1), date(2026, 8, 31), "600", status="DRAFT")
        void = self.bill(date(2026, 7, 1), date(2026, 7, 31), "500", status="VOID")
        sep = self.sep("900")

        payment = self.pay("1000")

        self.assertEqual([r.bill_id for r in payment.allocation_rows], [sep.pk])
        self.assertEqual(self.paid_on(draft), D("0"))
        self.assertEqual(self.paid_on(void), D("0"))

    def test_every_payment_method_is_accepted(self):
        for method in ("CASH", "UPI", "BANK_TRANSFER", "CHEQUE", "OTHER"):
            with self.subTest(method=method):
                payment = self.pay("10", payment_method=method, reference_number="R1")
                self.assertEqual(payment.payment_method, method)
                self.assertEqual(payment.reference_number, "R1")

    def test_invalid_method_and_amounts_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.pay("10", payment_method="BARTER")
        for bad in ("0", "-5", "abc", "1.005", "NaN", "Infinity", "1000000000000", None):
            with self.subTest(amount=bad), self.assertRaises(ValidationError):
                self.pay(bad)
        self.assertEqual(CustomerPayment.objects.count(), 0)

    def test_paid_at_rules(self):
        now = timezone.now()

        with self.assertRaises(ValidationError):  # no timezone
            self.pay("10", paid_at=datetime(2026, 9, 1, 10, 0))
        with self.assertRaises(ValidationError):  # in the future
            self.pay("10", paid_at=now + timedelta(hours=1))

        self.pay("10", paid_at=now + timedelta(minutes=2))  # clock skew is fine
        old = self.pay("10", paid_at=now - timedelta(days=30))  # backdating is fine
        self.assertEqual(old.paid_at, now - timedelta(days=30))

        defaulted = self.pay("10")
        self.assertLess(abs(defaulted.paid_at - timezone.now()), timedelta(seconds=30))

    def test_payment_numbers_are_generated_and_never_reused(self):
        first = self.pay("10")
        second = self.pay("10")
        other = self.pay("10", customer=self.customer2)

        self.assertRegex(first.payment_number, r"^PAY-C001-\d{6}-1$")
        self.assertTrue(second.payment_number.endswith("-2"))
        self.assertRegex(other.payment_number, r"^PAY-C002-\d{6}-1$")
        self.assertEqual(
            len({first.payment_number, second.payment_number, other.payment_number}),
            3,
        )

    def test_number_date_is_the_business_local_day(self):
        type(self.business).objects.filter(pk=self.business.pk).update(
            timezone="Pacific/Auckland",
        )
        # 17:30 on 30 Sep in Kolkata, but already 1 Oct in Auckland (+13).
        at = datetime(2026, 9, 30, 12, 0, tzinfo=ZoneInfo("UTC"))

        payment = self.pay("10", paid_at=at)

        self.assertTrue(payment.payment_number.startswith("PAY-C001-261001-"))

    def test_inactive_customer_can_still_pay(self):
        Customer.objects.filter(pk=self.customer.pk).update(is_active=False)
        self.sep("900")

        payment = self.pay("900")

        self.assertEqual(len(payment.allocation_rows), 1)

    def test_unknown_deleted_or_foreign_customers_are_rejected(self):
        Customer.objects.filter(pk=self.customer2.pk).update(is_deleted=True)

        for customer_id in (uuid4(), self.customer2.pk, self.other_customer.pk):
            with self.subTest(customer=customer_id), self.assertRaises(ValidationError):
                record_customer_payment(
                    business_id=self.business.pk, customer_id=customer_id,
                    amount="10", user=self.owner,
                )
        self.assertEqual(CustomerPayment.objects.count(), 0)

    def test_roles(self):
        self.pay("10", user=self.manager)
        for user in (self.staff, self.outsider):
            with self.subTest(user=user.username), self.assertRaises(PermissionDenied):
                self.pay("10", user=user)


class ExplicitAllocationTests(PaymentFixtures, TestCase):
    def test_pays_exactly_the_listed_bills_and_keeps_the_rest_as_advance(self):
        aug, sep = self.aug("600"), self.sep("900")

        payment = self.pay("500", allocations=[self.alloc(sep, "300")])

        self.assertEqual(self.paid_on(sep), D("300.00"))
        self.assertEqual(self.paid_on(aug), D("0"))  # not oldest-first here
        self.assertEqual(self.amounts(payment)["unallocated"], D("200.00"))
        self.assert_reconciled()

    def test_empty_list_keeps_everything_as_advance(self):
        self.sep("900")

        payment = self.pay("500", allocations=[])

        self.assertEqual(payment.allocation_rows, [])
        self.assertEqual(self.amounts(payment)["unallocated"], D("500.00"))

    def test_bad_allocations_are_rejected_and_nothing_is_written(self):
        sep = self.sep("900")
        draft = self.bill(date(2026, 8, 1), date(2026, 8, 31), "100", status="DRAFT")
        void = self.bill(date(2026, 7, 1), date(2026, 7, 31), "100", status="VOID")
        others = self.bill(
            date(2026, 9, 1), date(2026, 9, 30), "100", customer=self.customer2,
        )
        foreign = self.bill(
            date(2026, 9, 1), date(2026, 9, 30), "100", customer=self.other_customer,
        )
        cases = {
            "more than the payment": [self.alloc(sep, "1001")],
            "more than the bill owes": [self.alloc(sep, "901")],
            "same bill twice": [self.alloc(sep, "100"), self.alloc(sep, "100")],
            "same bill as uuid and text": [
                {"bill_id": sep.pk, "amount": "100"}, self.alloc(sep, "100"),
            ],
            "a draft": [self.alloc(draft, "10")],
            "a void bill": [self.alloc(void, "10")],
            "another customer's bill": [self.alloc(others, "10")],
            "another business's bill": [self.alloc(foreign, "10")],
            "an unknown bill": [{"bill_id": str(uuid4()), "amount": "10"}],
            "no bill id": [{"amount": "10"}],
            "not a bill id": [{"bill_id": "nope", "amount": "10"}],
            "zero share": [self.alloc(sep, "0")],
            "negative share": [self.alloc(sep, "-5")],
            "not a dict": ["x"],
        }
        for name, allocations in cases.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                self.pay("1000", allocations=allocations)

        self.assertEqual(CustomerPayment.objects.count(), 0)
        self.assertEqual(CustomerPaymentAllocation.objects.count(), 0)


class IdempotencyTests(PaymentFixtures, TestCase):
    def test_identical_retry_replays_without_new_rows(self):
        self.sep("900")
        first = self.pay("500", client_ref="r-1", notes="door")

        again = self.pay("500", client_ref="r-1", notes="door")

        self.assertTrue(again.replayed)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(CustomerPayment.objects.count(), 1)
        self.assertEqual(CustomerPaymentAllocation.objects.count(), 1)

    def test_same_ref_with_different_data_conflicts(self):
        self.pay("500", client_ref="r-1")
        changes = (
            dict(amount="501"),
            dict(payment_method="UPI"),
            dict(reference_number="X"),
            dict(notes="other"),
        )
        for change in changes:
            with self.subTest(change), self.assertRaises(ClientRefConflict):
                self.pay(**{"amount": "500", "client_ref": "r-1", **change})
        with self.assertRaises(ClientRefConflict):  # another customer
            self.pay("500", client_ref="r-1", customer=self.customer2)
        self.assertEqual(CustomerPayment.objects.count(), 1)

    def test_a_defaulted_time_does_not_make_a_retry_look_different(self):
        first = self.pay("500", client_ref="r-1")  # paid_at = the server's now

        self.assertTrue(self.pay("500", client_ref="r-1").replayed)
        # An explicit but different time is a different request.
        with self.assertRaises(ClientRefConflict):
            self.pay("500", client_ref="r-1", paid_at=first.paid_at - timedelta(days=1))
        # The very same explicit time replays.
        self.assertTrue(self.pay("500", client_ref="r-1", paid_at=first.paid_at).replayed)

    def test_explicit_allocations_are_compared_on_retry(self):
        sep = self.sep("900")
        self.pay("500", client_ref="r-1", allocations=[self.alloc(sep, "300")])

        same = self.pay("500", client_ref="r-1", allocations=[self.alloc(sep, "300")])
        self.assertTrue(same.replayed)
        auto = self.pay("500", client_ref="r-1")  # omitted = auto: not compared
        self.assertTrue(auto.replayed)
        for other in ([self.alloc(sep, "200")], []):
            with self.subTest(other), self.assertRaises(ClientRefConflict):
                self.pay("500", client_ref="r-1", allocations=other)

    def test_blank_ref_means_no_ref(self):
        self.pay("10", client_ref="  ")
        self.pay("10", client_ref="")
        self.assertEqual(CustomerPayment.objects.count(), 2)


class AllocateAdvanceTests(PaymentFixtures, TestCase):
    def allocate(self, payment, bill, amount, user=None):
        return allocate_customer_advance(
            business_id=self.business.pk,
            payment_id=payment.pk,
            bill_id=bill.pk,
            amount=amount,
            user=user or self.owner,
        )

    def test_advance_paid_before_the_bill_can_be_allocated_later(self):
        payment = self.pay("500")  # no bill yet
        sep = self.sep("900")

        allocation = self.allocate(payment, sep, "400")

        self.assertEqual(allocation.amount, D("400.00"))
        self.assertEqual(self.amounts(payment)["unallocated"], D("100.00"))
        position = self.assert_reconciled()
        self.assertEqual(position["outstanding_amount"], D("500.00"))
        self.assertEqual(position["available_credit"], D("100.00"))

    def test_identical_retry_replays_a_different_amount_is_refused(self):
        payment = self.pay("500")
        sep = self.sep("900")
        first = self.allocate(payment, sep, "400")

        again = self.allocate(payment, sep, "400")
        self.assertTrue(again.replayed)
        self.assertEqual(again.pk, first.pk)
        with self.assertRaises(ValidationError):
            self.allocate(payment, sep, "50")

    def test_limits(self):
        payment = self.pay("500")
        sep = self.sep("300")
        other = self.bill(date(2026, 8, 1), date(2026, 8, 31), "999", customer=self.customer2)
        draft = self.bill(date(2026, 7, 1), date(2026, 7, 31), "100", status="DRAFT")
        void = self.bill(date(2026, 6, 1), date(2026, 6, 30), "100", status="VOID")
        big = self.bill(date(2026, 5, 1), date(2026, 5, 31), "5000")

        cases = {
            "more than the bill owes": (sep, "301"),
            "more than the advance": (big, "501"),
            "another customer's bill": (other, "10"),
            "a draft": (draft, "10"),
            "a void bill": (void, "10"),
            "zero": (sep, "0"),
        }
        for name, (bill, amount) in cases.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                self.allocate(payment, bill, amount)

        with self.assertRaises(ValidationError):  # unknown bill
            allocate_customer_advance(
                business_id=self.business.pk, payment_id=payment.pk,
                bill_id=uuid4(), amount="10", user=self.owner,
            )
        with self.assertRaises(ValidationError):  # unknown payment
            allocate_customer_advance(
                business_id=self.business.pk, payment_id=uuid4(),
                bill_id=sep.pk, amount="10", user=self.owner,
            )
        self.assertEqual(CustomerPaymentAllocation.objects.count(), 0)

    def test_another_business_cannot_reach_the_payment_or_bill(self):
        payment = self.pay("500")
        sep = self.sep("900")

        with self.assertRaises((ValidationError, PermissionDenied)):
            allocate_customer_advance(
                business_id=self.other_business.pk, payment_id=payment.pk,
                bill_id=sep.pk, amount="10", user=self.other_owner,
            )
        self.assertEqual(CustomerPaymentAllocation.objects.count(), 0)

    def test_a_fully_reversed_payment_cannot_be_allocated(self):
        payment = self.pay("500")
        reverse_customer_payment(
            business_id=self.business.pk, payment_id=payment.pk,
            reason="bounced", user=self.owner,
        )
        with self.assertRaises(ValidationError):
            self.allocate(payment, self.sep("900"), "10")

    def test_roles(self):
        payment = self.pay("500")
        sep = self.sep("900")
        for user in (self.staff, self.outsider):
            with self.subTest(user=user.username), self.assertRaises(PermissionDenied):
                self.allocate(payment, sep, "10", user=user)
        self.allocate(payment, sep, "10", user=self.manager)


class ReversalTests(PaymentFixtures, TestCase):
    def reverse(self, payment, amount=None, reason="mistake", **kwargs):
        return reverse_customer_payment(
            business_id=self.business.pk, payment_id=payment.pk,
            amount=amount, reason=reason, user=self.owner, **kwargs,
        )

    def two_bill_payment(self):
        aug, sep = self.aug("600"), self.sep("900")
        payment = self.pay("1500")  # aug 600 then sep 900
        return payment, aug, sep

    def test_full_reversal_unwinds_every_allocation_explicitly(self):
        payment, aug, sep = self.two_bill_payment()

        reversal = self.reverse(payment)

        self.assertEqual(reversal.amount, D("1500.00"))
        self.assertEqual(
            {(u.allocation.bill_id, u.amount) for u in reversal.unwound},
            {(aug.pk, D("600.00")), (sep.pk, D("900.00"))},
        )
        # The allocation rows themselves are untouched.
        self.assertEqual(CustomerPaymentAllocation.objects.count(), 2)
        position = self.assert_reconciled()
        self.assertEqual(position["outstanding_amount"], D("1500.00"))
        self.assertEqual(position["available_credit"], D("0.00"))
        self.assertEqual(self.amounts(payment)["net"], D("0.00"))

    def test_the_advance_is_reversed_before_any_allocation(self):
        sep = self.sep("900")
        payment = self.pay("1500", allocations=[self.alloc(sep, "900")])  # 600 advance

        reversal = self.reverse(payment, "500")
        self.assertEqual(reversal.unwound, [])
        self.assertEqual(self.amounts(payment)["unallocated"], D("100.00"))

        mixed = self.reverse(payment, "300")  # 100 advance + 200 unwound
        self.assertEqual([u.amount for u in mixed.unwound], [D("200.00")])
        self.assertEqual(self.paid_on(sep), D("900.00"))  # row unchanged
        position = self.assert_reconciled()
        self.assertEqual(position["outstanding_amount"], D("200.00"))
        self.assertEqual(position["available_credit"], D("0.00"))

    def test_the_newest_allocation_is_unwound_first(self):
        payment, aug, sep = self.two_bill_payment()

        reversal = self.reverse(payment, "1000")

        taken = {u.allocation.bill_id: u.amount for u in reversal.unwound}
        self.assertEqual(taken, {sep.pk: D("900.00"), aug.pk: D("100.00")})

    def test_reversals_never_exceed_the_payment(self):
        payment = self.pay("1000")
        self.reverse(payment, "600")

        with self.assertRaises(ValidationError):
            self.reverse(payment, "401")
        self.reverse(payment)  # omitted amount = what is left (400)
        with self.assertRaises(ValidationError):
            self.reverse(payment)  # already fully reversed
        self.assertEqual(self.amounts(payment)["net"], D("0.00"))

    def test_invalid_amounts_and_reason(self):
        payment = self.pay("1000")
        for bad in ("0", "-5", "abc", "1.005"):
            with self.subTest(amount=bad), self.assertRaises(ValidationError):
                self.reverse(payment, bad)
        for reason in ("", "   ", None):
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                self.reverse(payment, reason=reason)
        self.assertEqual(CustomerPaymentReversal.objects.count(), 0)

    def test_allocation_on_a_void_bill_is_ignored_not_unwound(self):
        sep = self.sep("900")
        payment = self.pay("900")  # fully applied to sep
        sep.status = "VOID"
        sep.voided_at = timezone.now()
        sep.void_reason = "wrong"
        sep.save(update_fields=["status", "voided_at", "void_reason", "updated_at"])

        # The 900 is back in credit, so reversing it is plain advance.
        self.assertEqual(self.amounts(payment)["unallocated"], D("900.00"))
        reversal = self.reverse(payment, "400")

        self.assertEqual(reversal.unwound, [])
        self.assertEqual(CustomerPaymentAllocationReversal.objects.count(), 0)
        self.assertEqual(self.assert_reconciled()["available_credit"], D("500.00"))

    def test_retry_with_the_same_ref_replays_a_different_request_conflicts(self):
        payment = self.pay("1000")
        first = self.reverse(payment, "300", client_ref="rv-1")

        again = self.reverse(payment, "300", client_ref="rv-1")
        self.assertTrue(again.replayed)
        self.assertEqual(again.pk, first.pk)
        self.assertTrue(self.reverse(payment, client_ref="rv-1").replayed)  # no amount
        for change in (dict(amount="301"), dict(reason="other")):
            with self.subTest(change), self.assertRaises(ClientRefConflict):
                self.reverse(payment, **{"amount": "300", "client_ref": "rv-1", **change})
        self.assertEqual(CustomerPaymentReversal.objects.count(), 1)

    def test_reversed_at_is_never_before_the_payment(self):
        payment = self.pay("1000", paid_at=timezone.now() - timedelta(days=3))

        reversal = self.reverse(payment)

        self.assertGreaterEqual(reversal.reversed_at, payment.paid_at)
        self.assertLess(abs(reversal.reversed_at - timezone.now()), timedelta(seconds=30))

    def test_unknown_payment_and_roles(self):
        payment = self.pay("1000")
        with self.assertRaises(ValidationError):
            reverse_customer_payment(
                business_id=self.business.pk, payment_id=uuid4(),
                reason="x", user=self.owner,
            )
        with self.assertRaises(ValidationError):  # another business's id
            reverse_customer_payment(
                business_id=self.other_business.pk, payment_id=payment.pk,
                reason="x", user=self.other_owner,
            )
        for user in (self.staff, self.outsider):
            with self.subTest(user=user.username), self.assertRaises(PermissionDenied):
                reverse_customer_payment(
                    business_id=self.business.pk, payment_id=payment.pk,
                    reason="x", user=user,
                )
        self.assertEqual(CustomerPaymentReversal.objects.count(), 0)


class PositionTests(PaymentFixtures, TestCase):
    def test_empty_customer(self):
        position = self.assert_reconciled()
        self.assertTrue(all(value == 0 for value in position.values()))

    def test_unpaid_bills_add_up(self):
        self.aug("600")
        self.sep("900")

        position = self.assert_reconciled()

        self.assertEqual(position["outstanding_amount"], D("1500.00"))
        self.assertEqual(position["net_balance"], D("1500.00"))

    def test_voiding_a_paid_bill_returns_the_money_to_credit(self):
        sep = self.sep("900")
        self.pay("900")
        self.assertEqual(self.assert_reconciled()["net_balance"], D("0.00"))

        sep.status = "VOID"
        sep.voided_at = timezone.now()
        sep.void_reason = "wrong delivery"
        sep.save(update_fields=["status", "voided_at", "void_reason", "updated_at"])

        position = self.assert_reconciled()
        self.assertEqual(position["total_billed"], D("0.00"))
        self.assertEqual(position["available_credit"], D("900.00"))
        self.assertEqual(position["net_balance"], D("-900.00"))

    def test_as_of_replays_history_even_after_a_later_void(self):
        t0 = timezone.now() - timedelta(days=10)
        sep = self.make_bill(
            status="FINALIZED", total="900", finalized_at=t0,
        )
        self.pay("500", paid_at=t0 + timedelta(days=3))

        def at(days):
            return self.position(as_of=t0 + timedelta(days=days))

        self.assertEqual(at(-1)["total_billed"], D("0.00"))   # not issued yet
        self.assertEqual(at(1)["outstanding_amount"], D("900.00"))
        self.assertEqual(at(1)["total_payments"], D("0.00"))
        self.assertEqual(at(4)["total_payments"], D("500.00"))
        self.assertEqual(at(4)["outstanding_amount"], D("400.00"))

        sep.status = "VOID"
        sep.voided_at = t0 + timedelta(days=6)
        sep.void_reason = "wrong delivery"
        sep.save(update_fields=["status", "voided_at", "void_reason", "updated_at"])

        # Before the void the bill still counts; after it, the money is credit.
        self.assertEqual(at(5)["total_billed"], D("900.00"))
        self.assertEqual(at(5)["outstanding_amount"], D("400.00"))
        after = self.assert_reconciled(as_of=t0 + timedelta(days=7))
        self.assertEqual(after["total_billed"], D("0.00"))
        self.assertEqual(after["available_credit"], D("500.00"))
        self.assertEqual(after["net_balance"], D("-500.00"))
        self.assertEqual(self.position(), after)  # nothing happened since
