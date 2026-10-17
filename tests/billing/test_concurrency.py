"""
Race tests that FORCE the bad interleaving (pattern of
tests/delivery/test_lock_invariants.py).

One transaction is paused inside its critical window, holding its locks; the
rival then runs. With the customer lock in place the rival blocks, the gate
times out, and the paused transaction continues, which is the serialized
outcome the invariants allow. Remove the matching lock and the invariant
assertion fails.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from decimal import Decimal
from threading import Event
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import TransactionTestCase

from apps.billing import bill_services, payment_services
from apps.billing.models import (
    CustomerBill,
    CustomerBillLine,
    CustomerPayment,
    CustomerPaymentAllocation,
)
from apps.billing.payment_services import (
    allocate_customer_advance,
    record_customer_payment,
    reverse_customer_payment,
)
from apps.deliveries.models import Delivery
from apps.deliveries.services import cancel_delivery, correct_delivery
from tests.billing.base import SEP, BillFlow, ist

User = get_user_model()
D = Decimal
GATE = 1.0  # seconds a paused transaction waits for its rival


def in_thread(fn):
    """Run fn on its own DB connection; ('ok', value) or ('rejected', exc)."""
    close_old_connections()
    try:
        return ("ok", fn())
    except ValidationError as exc:
        return ("rejected", exc)
    finally:
        close_old_connections()


class RaceBase(BillFlow, TransactionTestCase):
    def owner_(self):
        return User.objects.get(pk=self.owner.pk)

    def race(self, module, name, leader, rival):
        """
        Run `leader`; pause it right after `module.name` returns (it still
        holds its locks), run `rival`, then let the leader finish.
        Returns (leader outcome, rival outcome).
        """
        paused, release = Event(), Event()
        real = getattr(module, name)

        def pausing(*args, **kwargs):
            result = real(*args, **kwargs)
            paused.set()
            release.wait(timeout=10)
            return result

        with mock.patch.object(module, name, pausing):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(in_thread, leader)
                self.assertTrue(paused.wait(timeout=10), "leader never paused")
                second = pool.submit(in_thread, rival)
                try:
                    second.result(timeout=GATE)  # returns at once if unlocked
                except FutureTimeout:
                    pass  # blocked by a lock: serialized, as intended
                release.set()
                return first.result(), second.result()

    def finalize_call(self, bill):
        return lambda: bill_services.finalize_bill(
            business_id=self.business.pk, bill_id=bill.pk, user=self.owner_(),
        )

    def assert_bill_matches_deliveries(self, bill):
        billed = set(
            CustomerBillLine.objects.filter(bill=bill).values_list("delivery_id", flat=True)
        )
        live = set(
            Delivery.objects.filter(
                customer=bill.customer, status="DELIVERED",
                delivered_at__date__gte=bill.period_start,
                delivered_at__date__lte=bill.period_end,
            ).values_list("pk", flat=True)
        )
        self.assertEqual(billed, live, "the bill and the deliveries disagree")


class FinalizeVersusDeliveryTests(RaceBase):
    def setUp(self):
        super().setUp()
        self.september()
        self.bill = self.draft()
        self.target = Delivery.objects.filter(product=self.cow).order_by("delivered_at").first()

    def run_race(self, rival):
        return self.race(
            bill_services, "build_bill_lines", self.finalize_call(self.bill), rival,
        )

    def test_a_delivery_cannot_slip_in_while_the_bill_is_being_frozen(self):
        add = lambda: self.deliver(self.ghee, "1", ist(2026, 9, 10), user=self.owner_())

        finalized, rival = self.run_race(add)

        self.assertEqual(finalized[0], "ok")
        self.assertEqual(rival[0], "rejected")
        self.assert_bill_matches_deliveries(self.bill)

    def test_a_delivery_cannot_be_cancelled_while_the_bill_is_being_frozen(self):
        cancel = lambda: cancel_delivery(
            business_id=self.business.pk, delivery_id=self.target.pk,
            user=self.owner_(), reason="mistake",
        )

        finalized, rival = self.run_race(cancel)

        self.assertEqual((finalized[0], rival[0]), ("ok", "rejected"))
        self.assert_bill_matches_deliveries(self.bill)

    def test_a_delivery_cannot_be_corrected_while_the_bill_is_being_frozen(self):
        correct = lambda: correct_delivery(
            business_id=self.business.pk, delivery_id=self.target.pk,
            user=self.owner_(), reason="mistake", quantity="5",
        )

        finalized, rival = self.run_race(correct)

        self.assertEqual((finalized[0], rival[0]), ("ok", "rejected"))
        self.assert_bill_matches_deliveries(self.bill)

    def test_two_finalizes_make_one_set_of_lines(self):
        finalized, rival = self.run_race(self.finalize_call(self.bill))

        self.assertEqual([finalized[0], rival[0]], ["ok", "ok"])
        self.assertEqual([finalized[1].replayed, rival[1].replayed], [False, True])
        self.assertEqual(CustomerBillLine.objects.filter(bill=self.bill).count(), 5)

    def test_void_waits_for_the_finalize_and_then_voids_it(self):
        void = lambda: bill_services.void_bill(
            business_id=self.business.pk, bill_id=self.bill.pk,
            user=self.owner_(), reason="changed my mind",
        )

        finalized, rival = self.run_race(void)

        self.assertEqual((finalized[0], rival[0]), ("ok", "ok"))
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.status, "VOID")
        self.assertEqual(CustomerBillLine.objects.filter(bill=self.bill).count(), 5)
        self.assertEqual(CustomerBill.objects.filter(replaces=self.bill).count(), 1)


class GenerateRaceTests(RaceBase):
    def test_two_generates_for_one_period_make_one_draft(self):
        self.september()
        generate = lambda: bill_services.generate_draft_bill(
            business_id=self.business.pk, customer_id=self.customer.pk,
            period_start=SEP[0], period_end=SEP[1], user=self.owner_(),
        )

        first, second = self.race(bill_services, "next_bill_number", generate, generate)

        self.assertEqual([first[0], second[0]], ["ok", "ok"])
        self.assertEqual([first[1].created, second[1].created], [True, False])
        self.assertEqual(CustomerBill.objects.count(), 1)


class PaymentRaceTests(RaceBase):
    def setUp(self):
        super().setUp()
        self.sep = self.make_bill(status="FINALIZED", total="900")

    def pay_call(self, amount, **kwargs):
        return lambda: record_customer_payment(
            business_id=self.business.pk, customer_id=self.customer.pk,
            amount=amount, user=self.owner_(), **kwargs,
        )

    def allocated(self, bill):
        return sum(
            (a.amount for a in CustomerPaymentAllocation.objects.filter(bill=bill)), D("0"),
        )

    def test_two_payments_never_over_pay_a_bill(self):
        first, second = self.race(
            payment_services, "next_payment_number",
            self.pay_call("700"), self.pay_call("700"),
        )

        self.assertEqual([first[0], second[0]], ["ok", "ok"])
        self.assertEqual(self.allocated(self.sep), D("900.00"))
        position = self.assert_reconciled()
        self.assertEqual(position["available_credit"], D("500.00"))

    def test_the_same_client_ref_makes_one_payment(self):
        pay = self.pay_call("100", client_ref="same")

        first, second = self.race(payment_services, "next_payment_number", pay, pay)

        self.assertEqual([first[1].replayed, second[1].replayed], [False, True])
        self.assertEqual(CustomerPayment.objects.count(), 1)

    def test_a_payment_is_never_allocated_to_a_bill_voided_meanwhile(self):
        void = lambda: bill_services.void_bill(
            business_id=self.business.pk, bill_id=self.sep.pk,
            user=self.owner_(), reason="wrong bill",
        )

        paid, voided = self.race(
            payment_services, "next_payment_number", self.pay_call("500"), void,
        )

        self.assertEqual((paid[0], voided[0]), ("ok", "ok"))
        self.sep.refresh_from_db()
        late = CustomerPaymentAllocation.objects.filter(
            bill=self.sep, created_at__gt=self.sep.voided_at,
        )
        self.assertFalse(late.exists(), "allocated to a bill after it was voided")
        self.assert_reconciled()

    def test_two_reversals_cannot_take_more_than_the_payment(self):
        payment = self.pay_call("1000", allocations=[])()
        reverse = lambda: reverse_customer_payment(
            business_id=self.business.pk, payment_id=payment.pk, amount="700",
            reason="bounced", user=self.owner_(),
        )

        first, second = self.race(payment_services, "_unwound_rows", reverse, reverse)

        self.assertEqual(sorted([first[0], second[0]]), ["ok", "rejected"])
        self.assertEqual(
            payment_services.selectors.get_payment_amounts(
                CustomerPayment.objects.get(pk=payment.pk)
            )["net"],
            D("300.00"),
        )

    def test_reverse_and_allocate_never_spend_the_same_advance(self):
        payment = self.pay_call("1000", allocations=[])()
        reverse = lambda: reverse_customer_payment(
            business_id=self.business.pk, payment_id=payment.pk, amount="700",
            reason="bounced", user=self.owner_(),
        )
        allocate = lambda: allocate_customer_advance(
            business_id=self.business.pk, payment_id=payment.pk,
            bill_id=self.sep.pk, amount="500", user=self.owner_(),
        )

        self.race(payment_services, "_unwound_rows", reverse, allocate)

        amounts = payment_services.selectors.get_payment_amounts(
            CustomerPayment.objects.get(pk=payment.pk)
        )
        self.assertGreaterEqual(amounts["unallocated"], 0)
        self.assertLessEqual(amounts["allocated"], amounts["net"])
