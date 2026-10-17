"""Bill lifecycle: draft, finalize, void, replacement, opening balance."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.billing.bill_services import (
    AlreadyBilled,
    BillConflict,
    CustomerNotFound,
    NothingToBill,
    OverlapsBilled,
    build_bill_lines,
    finalize_bill,
    generate_draft_bill,
    generate_drafts,
    record_opening_balance,
    void_bill,
)
from apps.billing.models import (
    CustomerBill,
    CustomerBillLine,
    CustomerPayment,
    CustomerPaymentAllocation,
)
from apps.businesses.models import Business
from apps.customers.models import Customer
from apps.deliveries.services import cancel_delivery, correct_delivery
from apps.inventory.models import BusinessMilkConfig
from apps.products.models import Product, ProductPrice
from tests.billing.base import PRICE_FROM, SEP, BillFlow, ist

D = Decimal


class DraftTests(BillFlow, TestCase):
    def test_draft_is_a_header_only_and_numbered(self):
        self.september()

        bill = self.draft()

        self.assertTrue(bill.created)
        self.assertEqual(bill.status, "DRAFT")
        self.assertEqual(bill.bill_number, "C001-260901-1")
        self.assertEqual(bill.total_amount, D("0.00"))
        self.assertEqual(self.lines(bill), [])  # lines are computed live

    def test_live_lines_cover_cow_buffalo_and_extras(self):
        total = self.september()
        business = Business.objects.get(pk=self.business.pk)

        rows, warnings = build_bill_lines(business, self.customer.pk, *SEP)

        kinds = [(r["description"], r["line_kind"]) for r in rows]
        self.assertEqual(kinds, [
            ("Cow Milk", "COW_MILK"),
            ("Cow Milk", "COW_MILK"),
            ("Buffalo Milk", "BUFFALO_MILK"),
            ("Cow Milk", "COW_MILK"),
            ("Cow Ghee", "OTHER"),
        ])
        self.assertEqual(sum(r["amount"] for r in rows), total)
        self.assertEqual(warnings, [])

    def test_a_missing_milk_mapping_is_reported_and_milk_falls_under_other(self):
        self.september()
        BusinessMilkConfig.objects.filter(business=self.business).update(
            buffalo_product=None,
        )
        business = Business.objects.get(pk=self.business.pk)

        rows, warnings = build_bill_lines(business, self.customer.pk, *SEP)

        self.assertEqual([w["code"] for w in warnings], ["MILK_CONFIG_INCOMPLETE"])
        kinds = {r["description"]: r["line_kind"] for r in rows}
        self.assertEqual(kinds["Buffalo Milk"], "OTHER")
        self.assertEqual(kinds["Cow Milk"], "COW_MILK")

    def test_price_changes_inside_the_period_keep_each_delivery_price(self):
        product = Product.objects.create(
            business=self.business, sku="SPL", name="Special Milk", unit="L",
        )
        switch = ist(2026, 9, 16, 0)
        for price, start, end in (("60.00", PRICE_FROM, switch), ("65.00", switch, None)):
            ProductPrice.objects.create(
                business=self.business, product=product, price_type="SELLING",
                unit_price=D(price), effective_from=start, effective_to=end,
            )
        self.deliver(product, "10", ist(2026, 9, 10))
        self.deliver(product, "10", ist(2026, 9, 20))
        business = Business.objects.get(pk=self.business.pk)

        rows, _ = build_bill_lines(business, self.customer.pk, *SEP)

        self.assertEqual([(r["unit_price"], r["amount"]) for r in rows], [
            (D("60.00"), D("600.00")), (D("65.00"), D("650.00")),
        ])

    def test_period_edges_are_half_open_in_the_business_timezone(self):
        self.deliver(self.cow, "1", ist(2026, 8, 31, 23, 59))     # before
        self.deliver(self.cow, "1", ist(2026, 9, 1, 0, 0))        # first instant
        self.deliver(self.cow, "1", ist(2026, 9, 30, 23, 59))     # last minute
        self.deliver(self.cow, "1", ist(2026, 10, 1, 0, 0))       # after
        business = Business.objects.get(pk=self.business.pk)

        rows, _ = build_bill_lines(business, self.customer.pk, *SEP)

        self.assertEqual(
            [r["delivered_at"].astimezone(ZoneInfo("Asia/Kolkata")).date() for r in rows],
            [date(2026, 9, 1), date(2026, 9, 30)],
        )

    def test_another_business_timezone_moves_the_edges(self):
        Business.objects.filter(pk=self.business.pk).update(
            timezone="America/New_York",
        )
        utc = ZoneInfo("UTC")
        # 22:00 on 31 Aug in New York, but already 1 Sep by the server clock.
        self.deliver(self.cow, "1", datetime(2026, 9, 1, 2, 0, tzinfo=utc))
        # 01:00 on 1 Sep and 23:59 on 30 Sep in New York.
        self.deliver(self.buffalo, "1", datetime(2026, 9, 1, 5, 0, tzinfo=utc))
        self.deliver(self.ghee, "1", datetime(2026, 10, 1, 3, 59, tzinfo=utc))
        # 00:00 on 1 Oct in New York: next period.
        self.deliver(self.cow, "1", datetime(2026, 10, 1, 4, 0, tzinfo=utc), shift="EVENING")
        business = Business.objects.get(pk=self.business.pk)

        rows, _ = build_bill_lines(business, self.customer.pk, *SEP)

        self.assertEqual(
            [r["description"] for r in rows], ["Buffalo Milk", "Cow Ghee"],
        )

    def test_custom_period_crosses_the_month(self):
        self.deliver(self.cow, "1", ist(2026, 9, 10))
        self.deliver(self.cow, "2", ist(2026, 9, 20))
        self.deliver(self.cow, "3", ist(2026, 10, 10))
        business = Business.objects.get(pk=self.business.pk)

        rows, _ = build_bill_lines(
            business, self.customer.pk, date(2026, 9, 15), date(2026, 10, 14),
        )

        self.assertEqual([r["quantity"] for r in rows], [D("2.000"), D("3.000")])

    def test_cancelled_deliveries_are_not_billed(self):
        keep = self.deliver(self.cow, "1", ist(2026, 9, 2))
        gone = self.deliver(self.cow, "5", ist(2026, 9, 3))
        cancel_delivery(
            business_id=self.business.pk, delivery_id=gone.pk,
            user=self.owner, reason="not delivered",
        )
        business = Business.objects.get(pk=self.business.pk)

        rows, _ = build_bill_lines(business, self.customer.pk, *SEP)

        self.assertEqual([r["delivery_id"] for r in rows], [keep.pk])

    def test_other_customers_deliveries_are_not_billed(self):
        self.deliver(self.cow, "1", ist(2026, 9, 2))
        self.deliver(self.cow, "9", ist(2026, 9, 2), customer=self.customer2)
        business = Business.objects.get(pk=self.business.pk)

        rows, _ = build_bill_lines(business, self.customer.pk, *SEP)

        self.assertEqual(len(rows), 1)

    def test_nothing_to_bill(self):
        with self.assertRaises(NothingToBill):
            self.draft()  # no deliveries at all
        gone = self.deliver(self.cow, "1", ist(2026, 9, 2))
        cancel_delivery(
            business_id=self.business.pk, delivery_id=gone.pk,
            user=self.owner, reason="not delivered",
        )
        with self.assertRaises(NothingToBill):
            self.draft()
        self.assertEqual(CustomerBill.objects.count(), 0)

    def test_generating_twice_returns_the_same_draft(self):
        self.september()
        first = self.draft()

        again = self.draft()

        self.assertFalse(again.created)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(CustomerBill.objects.count(), 1)

    def test_a_billed_or_overlapping_period_cannot_get_another_draft(self):
        self.september()
        bill = self.draft()
        self.finalize(bill)

        with self.assertRaises(AlreadyBilled):
            self.draft()
        with self.assertRaises(OverlapsBilled):
            self.draft(period=(date(2026, 9, 15), date(2026, 10, 14)))
        # A different customer is unaffected.
        self.september(self.customer2)
        self.draft(customer=self.customer2)

    def test_drafts_may_overlap_each_other_until_one_is_finalized(self):
        self.september()
        self.deliver(self.cow, "1", ist(2026, 10, 5))
        first = self.draft()
        second = self.draft(period=(date(2026, 9, 15), date(2026, 10, 14)))

        self.assertNotEqual(first.pk, second.pk)
        self.finalize(first)
        with self.assertRaises(OverlapsBilled):
            self.finalize(second)

    def test_period_validation(self):
        self.september()
        bad = [
            (date(2026, 9, 30), date(2026, 9, 1)),
            (date(2026, 1, 1), date(2027, 1, 2)),        # 367 days; 366 is the limit
            ("2026-09-01", date(2026, 9, 30)),
            (date(2026, 9, 1), None),
            (datetime(2026, 9, 1, 10, 0), date(2026, 9, 30)),
        ]
        for period in bad:
            with self.subTest(period=period), self.assertRaises(ValidationError):
                self.draft(period=period)
        self.draft(period=(date(2026, 1, 1), date(2026, 12, 31)))  # 365 days: fine

    def test_unknown_foreign_or_deleted_customers(self):
        self.september()
        Customer.objects.filter(pk=self.customer2.pk).update(is_deleted=True)
        for customer_id in (uuid4(), self.other_customer.pk, self.customer2.pk):
            with self.subTest(customer=customer_id), self.assertRaises(CustomerNotFound):
                generate_draft_bill(
                    business_id=self.business.pk, customer_id=customer_id,
                    period_start=SEP[0], period_end=SEP[1], user=self.owner,
                )

    def test_inactive_customer_can_still_be_billed(self):
        self.september()
        Customer.objects.filter(pk=self.customer.pk).update(is_active=False)
        self.assertTrue(self.draft().created)

    def test_numbers_are_never_reused(self):
        self.september()
        first = self.draft()
        self.void(first, reason="discard it")  # a draft: discarded, no replacement

        second = self.draft()

        self.assertEqual(first.bill_number, "C001-260901-1")
        self.assertEqual(second.bill_number, "C001-260901-2")

    def test_roles(self):
        self.september()
        self.draft(user=self.manager)
        for user in (self.staff, self.outsider):
            with self.subTest(user=user.username), self.assertRaises(PermissionDenied):
                self.draft(user=user)


class FinalizeTests(BillFlow, TestCase):
    def test_finalize_freezes_lines_total_and_snapshot(self):
        total = self.september()
        bill = self.draft()

        done = self.finalize(bill)

        self.assertEqual(done.status, "FINALIZED")
        self.assertEqual(done.total_amount, total)
        self.assertEqual(done.balance_before, D("0.00"))
        self.assertEqual(done.finalized_by, self.owner)
        self.assertIsNotNone(done.finalized_at)
        self.assertFalse(done.replayed)
        self.assertEqual(done.credit_applied, [])
        self.assertEqual(done.header_snapshot["business"]["name"], "Bill Dairy")
        self.assertEqual(done.header_snapshot["customer"]["name"], "Ravi")
        self.assertEqual(done.header_snapshot["customer"]["address"], "12 Main Road")

        lines = self.lines(done)
        self.assertEqual(len(lines), 5)
        self.assertEqual(sum(l.amount for l in lines), total)
        cow = lines[0]
        self.assertEqual(
            (cow.line_kind, cow.description, cow.unit, cow.quantity, cow.unit_price, cow.amount),
            ("COW_MILK", "Cow Milk", "L", D("2.000"), D("60.00"), D("120.00")),
        )
        position = self.assert_reconciled()
        self.assertEqual(position["total_billed"], total)
        self.assertEqual(position["net_balance"], total)

    def test_finalizing_again_replays(self):
        self.september()
        bill = self.draft()
        first = self.finalize(bill)

        again = self.finalize(bill)

        self.assertTrue(again.replayed)
        self.assertEqual(again.finalized_at, first.finalized_at)
        self.assertEqual(CustomerBillLine.objects.count(), 5)
        with self.assertRaises(BillConflict):  # a replay still checks what you reviewed
            self.finalize(bill, expected_total="1.00")
        self.finalize(bill, expected_total=str(first.total_amount))

    def test_expected_total_protects_what_was_reviewed(self):
        total = self.september()
        bill = self.draft()
        extra = self.deliver(self.cow, "1", ist(2026, 9, 10))  # after the review

        with self.assertRaises(BillConflict):
            self.finalize(bill, expected_total=str(total))

        bill.refresh_from_db()
        self.assertEqual(bill.status, "DRAFT")
        self.assertEqual(CustomerBillLine.objects.count(), 0)
        done = self.finalize(bill, expected_total=str(total + D("60.00")))
        self.assertIn(extra.pk, [l.delivery_id for l in self.lines(done)])

    def test_the_period_must_have_ended(self):
        today = timezone.now().astimezone(ZoneInfo("Asia/Kolkata")).date()
        self.deliver(self.cow, "1", timezone.now())
        month = (today.replace(day=1), today + timedelta(days=1))
        bill = self.draft(period=month)  # a draft preview is fine

        with self.assertRaises(ValidationError):
            self.finalize(bill)  # the period still runs through tomorrow
        ends_today = self.draft(period=(today, today))
        with self.assertRaises(ValidationError):
            self.finalize(ends_today)

    def test_yesterday_can_be_billed(self):
        today = timezone.now().astimezone(ZoneInfo("Asia/Kolkata")).date()
        yesterday = today - timedelta(days=1)
        self.deliver(self.cow, "1", timezone.now() - timedelta(days=1))
        bill = self.draft(period=(yesterday, yesterday))

        self.assertEqual(self.finalize(bill).status, "FINALIZED")

    def test_deliveries_cancelled_before_finalize_leave_nothing_to_bill(self):
        delivery = self.deliver(self.cow, "1", ist(2026, 9, 2))
        bill = self.draft()
        cancel_delivery(
            business_id=self.business.pk, delivery_id=delivery.pk,
            user=self.owner, reason="not delivered",
        )

        with self.assertRaises(NothingToBill):
            self.finalize(bill)
        bill.refresh_from_db()
        self.assertEqual(bill.status, "DRAFT")

    def test_a_finalized_bill_does_not_follow_later_changes(self):
        self.september()
        bill = self.finalize(self.draft())
        ProductPrice.objects.filter(product=self.cow).update(
            effective_to=ist(2026, 9, 20),
        )
        ProductPrice.objects.create(
            business=self.business, product=self.cow, price_type="SELLING",
            unit_price=D("99.00"), effective_from=ist(2026, 9, 20),
        )
        self.deliver(self.cow, "1", ist(2026, 9, 25))  # a new delivery in the period

        bill.refresh_from_db()
        self.assertEqual(bill.total_amount, D("1240.00"))
        self.assertEqual(len(self.lines(bill)), 5)
        with self.assertRaises(ValueError):
            bill.total_amount = D("1.00")
            bill.save()

    def test_total_is_the_sum_of_stored_delivery_totals_not_quantity_times_rate(self):
        product = self.product("SP", "Special", "60.55")
        for day in (1, 2, 3):
            self.deliver(product, "0.333", ist(2026, 9, day))
        # each delivery rounds 20.16315 to 20.16; 0.999 x 60.55 would say 60.49.
        done = self.finalize(self.draft())

        self.assertEqual([l.amount for l in self.lines(done)], [D("20.16")] * 3)
        self.assertEqual(done.total_amount, D("60.48"))

    def test_held_advance_is_applied_to_the_new_bill(self):
        total = self.september()  # 1240
        advance = self.pay("2000")

        done = self.finalize(self.draft())

        self.assertEqual(done.balance_before, D("-2000.00"))
        self.assertEqual([a.amount for a in done.credit_applied], [total])
        position = self.assert_reconciled()
        self.assertEqual(position["outstanding_amount"], D("0.00"))
        self.assertEqual(position["available_credit"], D("760.00"))
        self.assertEqual(position["net_balance"], D("-760.00"))
        self.assertEqual(self.paid_on(done), total)
        self.assertEqual(CustomerPayment.objects.get(pk=advance.pk).amount, D("2000.00"))

    def test_the_clients_example_900_bill_1000_paid_then_600_next_month(self):
        self.deliver(self.cow, "15", ist(2026, 8, 10))                 # 900
        august = self.finalize(self.draft(period=(date(2026, 8, 1), date(2026, 8, 31))))
        self.assertEqual(august.total_amount, D("900.00"))
        self.pay("1000")
        self.assertEqual(self.assert_reconciled()["net_balance"], D("-100.00"))

        self.deliver(self.cow, "10", ist(2026, 9, 10))                 # 600
        september = self.finalize(self.draft())

        self.assertEqual(september.balance_before, D("-100.00"))
        self.assertEqual([a.amount for a in september.credit_applied], [D("100.00")])
        position = self.assert_reconciled()
        self.assertEqual(position["net_balance"], D("500.00"))         # 600 - 100
        self.assertEqual(position["outstanding_amount"], D("500.00"))
        self.assertEqual(position["available_credit"], D("0.00"))

    def test_balance_before_includes_what_was_already_owed(self):
        self.deliver(self.cow, "5", ist(2026, 8, 10))                  # 300
        self.finalize(self.draft(period=(date(2026, 8, 1), date(2026, 8, 31))))
        self.deliver(self.cow, "5", ist(2026, 9, 10))

        september = self.finalize(self.draft())

        self.assertEqual(september.balance_before, D("300.00"))

    def test_classification_is_snapshotted(self):
        self.september()
        done = self.finalize(self.draft())
        BusinessMilkConfig.objects.filter(business=self.business).update(
            cow_product=None, buffalo_product=None,
        )

        kinds = sorted({l.line_kind for l in self.lines(done)})

        self.assertEqual(kinds, ["BUFFALO_MILK", "COW_MILK", "OTHER"])

    def test_finalize_reports_a_missing_mapping(self):
        self.september()
        BusinessMilkConfig.objects.filter(business=self.business).delete()

        done = self.finalize(self.draft())

        self.assertEqual([w["code"] for w in done.warnings], ["MILK_CONFIG_INCOMPLETE"])
        self.assertEqual({l.line_kind for l in self.lines(done)}, {"OTHER"})

    def test_unknown_foreign_and_void_bills(self):
        self.september()
        bill = self.draft()
        with self.assertRaises(ValidationError):
            finalize_bill(
                business_id=self.business.pk, bill_id=uuid4(), user=self.owner,
            )
        with self.assertRaises(ValidationError):  # another business's view of it
            finalize_bill(
                business_id=self.other_business.pk, bill_id=bill.pk,
                user=self.other_owner,
            )
        self.void(bill, reason="discard it")
        with self.assertRaises(ValidationError):
            self.finalize(bill)

    def test_roles(self):
        self.september()
        bill = self.draft()
        for user in (self.staff, self.outsider):
            with self.subTest(user=user.username), self.assertRaises(PermissionDenied):
                self.finalize(bill, user=user)
        self.finalize(bill, user=self.manager)


class VoidTests(BillFlow, TestCase):
    def finalized(self):
        total = self.september()
        return self.finalize(self.draft()), total

    def test_void_keeps_the_bill_and_issues_a_replacement_draft(self):
        bill, total = self.finalized()
        lines_before = [(l.pk, l.amount) for l in self.lines(bill)]

        voided, replacement = self.void(bill, reason="wrong delivery")

        self.assertEqual(voided.status, "VOID")
        self.assertEqual(voided.void_reason, "wrong delivery")
        self.assertEqual(voided.voided_by, self.owner)
        self.assertIsNotNone(voided.voided_at)
        self.assertEqual(voided.total_amount, total)       # the original is intact
        self.assertEqual([(l.pk, l.amount) for l in self.lines(voided)], lines_before)
        self.assertEqual(replacement.status, "DRAFT")
        self.assertEqual(replacement.replaces_id, bill.pk)
        self.assertEqual(replacement.bill_number, "C001-260901-2")
        self.assertEqual(
            (replacement.period_start, replacement.period_end), SEP,
        )
        self.assertEqual(self.position()["total_billed"], D("0.00"))

    def test_a_reason_is_required(self):
        bill, _ = self.finalized()
        for reason in ("", "  ", "ab", None):
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                self.void(bill, reason=reason)
        bill.refresh_from_db()
        self.assertEqual(bill.status, "FINALIZED")

    def test_replacement_is_optional(self):
        bill, _ = self.finalized()

        voided, replacement = self.void(bill, create_replacement=False)

        self.assertIsNone(replacement)
        self.assertEqual(CustomerBill.objects.count(), 1)

    def test_a_replacement_is_not_offered_for_a_draft_or_opening_balance(self):
        self.september()
        draft = self.draft()
        with self.assertRaises(ValidationError):
            self.void(draft, create_replacement=True)
        voided, replacement = self.void(draft)          # default: none
        self.assertIsNone(replacement)

        opening = record_opening_balance(
            business_id=self.business.pk, customer_id=self.customer.pk,
            amount="500", as_of_date=date(2026, 8, 31), user=self.owner,
        ).bill
        with self.assertRaises(ValidationError):
            self.void(opening, create_replacement=True)
        self.assertIsNone(self.void(opening)[1])

    def test_voiding_twice_is_refused(self):
        bill, _ = self.finalized()
        self.void(bill)
        with self.assertRaises(ValidationError):
            self.void(bill)

    def test_payments_survive_a_void_and_are_not_counted_twice(self):
        # Pay the bill, find a mistake, void, fix the delivery, re-issue.
        self.deliver(self.cow, "10", ist(2026, 9, 10))                 # 600
        original = self.finalize(self.draft())
        self.pay("600")
        self.assertEqual(self.assert_reconciled()["net_balance"], D("0.00"))
        allocations_before = CustomerPaymentAllocation.objects.count()

        voided, replacement = self.void(original, reason="quantity was 9")
        delivery = original.lines.first().delivery
        correct_delivery(
            business_id=self.business.pk, delivery_id=delivery.pk,
            user=self.owner, reason="quantity was 9", quantity="9",
        )

        # The payment is untouched; its 600 is advance again.
        self.assertEqual(CustomerPayment.objects.count(), 1)
        self.assertEqual(CustomerPaymentAllocation.objects.count(), allocations_before)
        position = self.assert_reconciled()
        self.assertEqual(position["available_credit"], D("600.00"))
        self.assertEqual(position["net_balance"], D("-600.00"))

        reissued = self.finalize(replacement)

        self.assertEqual(reissued.total_amount, D("540.00"))
        self.assertEqual(reissued.balance_before, D("-600.00"))
        self.assertEqual(self.paid_on(reissued), D("540.00"))
        position = self.assert_reconciled()
        self.assertEqual(position["total_billed"], D("540.00"))
        self.assertEqual(position["available_credit"], D("60.00"))
        self.assertEqual(position["net_balance"], D("-60.00"))
        self.assertEqual(voided.status, "VOID")

    def test_replacements_chain(self):
        bill, _ = self.finalized()
        _, second = self.void(bill)
        third_source = self.finalize(second)
        _, third = self.void(third_source, reason="again wrong")

        self.assertEqual(third.bill_number, "C001-260901-3")
        self.assertEqual(third.replaces_id, third_source.pk)
        self.assertEqual(second.replaces_id, bill.pk)

    def test_roles(self):
        bill, _ = self.finalized()
        for user in (self.staff, self.outsider):
            with self.subTest(user=user.username), self.assertRaises(PermissionDenied):
                self.void(bill, user=user)
        self.void(bill, user=self.manager)


class OpeningBalanceTests(BillFlow, TestCase):
    def opening(self, amount, as_of=date(2026, 8, 31), **kwargs):
        return record_opening_balance(
            business_id=self.business.pk, customer_id=self.customer.pk,
            amount=amount, as_of_date=as_of, user=self.owner, **kwargs,
        )

    def test_an_owed_amount_becomes_a_finalized_opening_bill(self):
        result = self.opening("1200")

        bill = result.bill
        self.assertEqual(result.kind, "BILL")
        self.assertFalse(result.replayed)
        self.assertEqual(bill.bill_type, "OPENING")
        self.assertEqual(bill.status, "FINALIZED")
        self.assertEqual(bill.bill_number, "C001-OB-260831-1")
        self.assertEqual((bill.period_start, bill.period_end), (date(2026, 8, 31),) * 2)
        self.assertEqual(bill.total_amount, D("1200.00"))
        self.assertEqual(bill.balance_before, D("0.00"))
        position = self.assert_reconciled()
        self.assertEqual(position["net_balance"], D("1200.00"))
        self.assertEqual(position["outstanding_amount"], D("1200.00"))

    def test_later_payments_settle_the_opening_balance_first(self):
        self.opening("1200")
        self.deliver(self.cow, "10", ist(2026, 9, 10))
        sep = self.finalize(self.draft())

        payment = self.pay("1300")

        self.assertEqual(payment.allocation_rows[0].amount, D("1200.00"))   # opening first
        self.assertEqual(payment.allocation_rows[1].bill_id, sep.pk)
        self.assertEqual(payment.allocation_rows[1].amount, D("100.00"))
        self.assertEqual(self.assert_reconciled()["outstanding_amount"], D("500.00"))

    def test_an_identical_retry_replays_a_different_amount_conflicts(self):
        first = self.opening("1200").bill

        again = self.opening("1200")
        self.assertTrue(again.replayed)
        self.assertEqual(again.bill.pk, first.pk)
        with self.assertRaises(BillConflict):
            self.opening("1300")
        with self.assertRaises(BillConflict):
            self.opening("1200", as_of=date(2026, 8, 30))
        self.assertEqual(CustomerBill.objects.count(), 1)

    def test_a_mistake_is_fixed_by_voiding_and_entering_again(self):
        wrong = self.opening("1200").bill
        void_bill(
            business_id=self.business.pk, bill_id=wrong.pk, reason="typed wrong",
            user=self.owner,
        )

        right = self.opening("1000").bill

        self.assertEqual(right.bill_number, "C001-OB-260831-2")
        self.assertEqual(self.assert_reconciled()["net_balance"], D("1000.00"))

    def test_a_prepaid_amount_becomes_an_advance_payment(self):
        result = self.opening("-300", client_ref="ob-1")

        payment = result.payment
        self.assertEqual(result.kind, "ADVANCE")
        self.assertIsNone(result.bill)
        self.assertEqual(payment.amount, D("300.00"))
        self.assertEqual(payment.payment_method, "OTHER")
        self.assertIn("2026-08-31", payment.notes)
        position = self.assert_reconciled()
        self.assertEqual(position["available_credit"], D("300.00"))
        self.assertEqual(position["net_balance"], D("-300.00"))
        self.assertTrue(self.opening("-300", client_ref="ob-1").replayed)
        self.assertEqual(CustomerPayment.objects.count(), 1)

    def test_the_advance_is_applied_to_the_next_bill(self):
        self.opening("-300")
        self.deliver(self.cow, "10", ist(2026, 9, 10))                 # 600

        done = self.finalize(self.draft())

        self.assertEqual(done.balance_before, D("-300.00"))
        self.assertEqual(self.assert_reconciled()["net_balance"], D("300.00"))

    def test_validation_and_roles(self):
        for amount in ("0", "abc", "1.005", None):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                self.opening(amount)
        with self.assertRaises(ValidationError):
            self.opening("100", as_of=timezone.now().date() + timedelta(days=30))
        with self.assertRaises(ValidationError):
            self.opening("100", as_of="2026-08-31")
        with self.assertRaises(CustomerNotFound):
            record_opening_balance(
                business_id=self.business.pk, customer_id=self.other_customer.pk,
                amount="100", as_of_date=date(2026, 8, 31), user=self.owner,
            )
        for user in (self.staff, self.outsider):
            with self.subTest(user=user.username), self.assertRaises(PermissionDenied):
                record_opening_balance(
                    business_id=self.business.pk, customer_id=self.customer.pk,
                    amount="100", as_of_date=date(2026, 8, 31), user=user,
                )
        self.assertEqual(CustomerBill.objects.count(), 0)


class BulkGenerateTests(BillFlow, TestCase):
    def generate(self, **kwargs):
        return generate_drafts(
            business_id=self.business.pk, period_start=SEP[0], period_end=SEP[1],
            user=self.owner, **kwargs,
        )

    def test_default_covers_every_customer_with_deliveries(self):
        self.september(self.customer2)    # Sita
        self.september(self.customer)     # Ravi

        result = self.generate()

        names = [b.customer.name for b in result["created"]]
        self.assertEqual(names, ["Ravi", "Sita"])     # by name
        self.assertEqual(result["existing"], [])
        self.assertEqual(result["skipped"], [])

        again = self.generate()
        self.assertEqual(again["created"], [])
        self.assertEqual(len(again["existing"]), 2)

    def test_customers_without_deliveries_are_not_listed_unless_asked_for(self):
        self.september(self.customer)
        self.assertEqual(self.generate()["skipped"], [])

        result = self.generate(customer_ids=[self.customer2.pk])

        self.assertEqual(
            result["skipped"],
            [{"customer_id": self.customer2.pk, "reason": "NO_DELIVERIES"}],
        )

    def test_already_billed_overlapping_and_unknown_customers_are_skipped(self):
        self.september(self.customer)
        self.september(self.customer2)
        billed = self.finalize(self.draft())
        Customer.objects.filter(pk=self.customer2.pk).update(is_deleted=True)
        stranger = uuid4()

        result = self.generate(
            customer_ids=[self.customer.pk, self.customer2.pk, self.other_customer.pk, stranger],
        )

        reasons = {s["customer_id"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(reasons, {
            self.customer.pk: "ALREADY_BILLED",
            self.customer2.pk: "CUSTOMER_NOT_FOUND",
            self.other_customer.pk: "CUSTOMER_NOT_FOUND",
            stranger: "CUSTOMER_NOT_FOUND",
        })
        self.assertEqual(result["created"], [])
        self.assertEqual(billed.status, "FINALIZED")

    def test_roles_and_validation(self):
        with self.assertRaises(PermissionDenied):
            generate_drafts(
                business_id=self.business.pk, period_start=SEP[0],
                period_end=SEP[1], user=self.staff,
            )
        with self.assertRaises(ValidationError):
            self.generate(customer_ids=["not-a-uuid"])
        with self.assertRaises(ValidationError):
            generate_drafts(
                business_id=self.business.pk, period_start=SEP[1],
                period_end=SEP[0], user=self.owner,
            )
