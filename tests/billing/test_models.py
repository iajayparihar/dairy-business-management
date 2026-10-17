"""Schema-level guarantees of the billing tables: constraints and immutability."""
from datetime import date
from decimal import Decimal

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.billing.models import (
    CustomerBill,
    CustomerBillLine,
    CustomerPayment,
    CustomerPaymentAllocation,
    CustomerPaymentAllocationReversal,
    CustomerPaymentReversal,
)
from apps.payments.models import SupplierPayment
from tests.billing.base import BillingBase, ist

VOID_UPDATE_FIELDS = ["status", "voided_at", "voided_by", "void_reason", "updated_at"]


class DbRejects:
    def assertDbRejects(self, create):
        """The database (not app code) must refuse this insert or update."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            create()


class BillConstraintTests(DbRejects, BillingBase, TestCase):
    def setUp(self):
        self.build()

    def test_bill_number_is_unique_per_business(self):
        self.make_bill(number="X-1")
        self.assertDbRejects(lambda: self.make_bill(
            number="X-1", start=date(2026, 8, 1), end=date(2026, 8, 31),
        ))
        # Another business may use the same number.
        self.make_bill(customer=self.other_customer, number="X-1")

    def test_one_live_bill_per_customer_and_exact_period(self):
        draft = self.make_bill()
        self.assertDbRejects(lambda: self.make_bill())
        self.assertDbRejects(lambda: self.make_bill(status="FINALIZED"))

        # A void bill frees the period for a replacement.
        draft.status = "VOID"
        draft.voided_at = timezone.now()
        draft.void_reason = "discarded"
        draft.save()
        self.make_bill()

    def test_finalized_periods_of_one_customer_never_overlap(self):
        self.make_bill(status="FINALIZED", start=date(2026, 9, 1), end=date(2026, 9, 15))
        # Sharing the 15th is an overlap (periods are inclusive).
        self.assertDbRejects(lambda: self.make_bill(
            status="FINALIZED", start=date(2026, 9, 15), end=date(2026, 9, 30),
        ))
        # Back to back is fine, and so is another customer's period.
        self.make_bill(status="FINALIZED", start=date(2026, 9, 16), end=date(2026, 9, 30))
        self.make_bill(
            customer=self.customer2, status="FINALIZED",
            start=date(2026, 9, 1), end=date(2026, 9, 30),
        )

    def test_draft_and_void_bills_may_overlap_a_finalized_one(self):
        self.make_bill(status="FINALIZED")
        self.make_bill(start=date(2026, 9, 10), end=date(2026, 9, 20))
        self.make_bill(status="VOID", start=date(2026, 9, 5), end=date(2026, 9, 25))

    def test_finalizing_an_overlapping_draft_is_refused_by_the_database(self):
        self.make_bill(status="FINALIZED", start=date(2026, 9, 1), end=date(2026, 9, 30))
        draft = self.make_bill(start=date(2026, 9, 20), end=date(2026, 10, 10))

        draft.status = "FINALIZED"
        draft.finalized_at = timezone.now()
        draft.balance_before = Decimal("0.00")
        self.assertDbRejects(draft.save)

    def test_state_must_match_the_stamps(self):
        now = timezone.now()
        bad = [
            dict(status="DRAFT", finalized_at=now),
            dict(status="DRAFT", voided_at=now),
            dict(status="FINALIZED", balance_before=None),
            dict(status="FINALIZED", finalized_at=None),
            dict(status="FINALIZED", voided_at=now),
            dict(status="VOID", voided_at=None),
            dict(status="VOID", void_reason=""),
        ]
        for index, fields in enumerate(bad):
            with self.subTest(fields=fields):
                self.assertDbRejects(lambda: self.make_bill(
                    start=date(2026, 1, 1 + index), end=date(2026, 1, 1 + index),
                    **fields,
                ))

    def test_period_and_total_checks(self):
        self.assertDbRejects(lambda: self.make_bill(
            start=date(2026, 9, 30), end=date(2026, 9, 1),
        ))
        self.assertDbRejects(lambda: self.make_bill(total="-0.01"))

    def test_opening_bill_shape(self):
        day = date(2026, 8, 31)
        # Single day only, never a draft.
        self.assertDbRejects(lambda: self.make_bill(
            bill_type="OPENING", status="FINALIZED", start=day, end=date(2026, 9, 1),
        ))
        self.assertDbRejects(lambda: self.make_bill(
            bill_type="OPENING", status="DRAFT", start=day, end=day,
        ))

        opening = self.make_bill(
            bill_type="OPENING", status="FINALIZED", start=day, end=day, total="500",
        )
        # Only one live opening balance per customer, until it is voided.
        self.assertDbRejects(lambda: self.make_bill(
            bill_type="OPENING", status="FINALIZED", start=day, end=day,
        ))
        opening.status = "VOID"
        opening.voided_at = timezone.now()
        opening.void_reason = "typed wrong"
        opening.save(update_fields=VOID_UPDATE_FIELDS)
        self.make_bill(bill_type="OPENING", status="FINALIZED", start=day, end=day)

    def test_opening_bill_does_not_block_period_bills(self):
        day = date(2026, 9, 1)
        self.make_bill(bill_type="OPENING", status="FINALIZED", start=day, end=day)
        self.make_bill(status="FINALIZED", start=day, end=date(2026, 9, 30))


class BillImmutabilityTests(BillingBase, TestCase):
    def setUp(self):
        self.build()

    def test_draft_can_be_edited(self):
        bill = self.make_bill()
        bill.total_amount = Decimal("10.00")
        bill.save()

    def test_finalized_bill_cannot_be_edited(self):
        bill = self.make_bill(status="FINALIZED", total="900")
        bill.total_amount = Decimal("800.00")
        with self.assertRaises(ValueError):
            bill.save()
        with self.assertRaises(ValueError):
            bill.save(update_fields=["total_amount"])
        bill.refresh_from_db()
        self.assertEqual(bill.total_amount, Decimal("900.00"))

    def test_finalized_bill_can_only_be_voided_through_the_void_fields(self):
        bill = self.make_bill(status="FINALIZED", total="900")
        bill.status = "VOID"
        bill.voided_at = timezone.now()
        bill.void_reason = "wrong delivery"

        with self.assertRaises(ValueError):  # no update_fields: could write anything
            bill.save()
        with self.assertRaises(ValueError):  # a non-void field slipped in
            bill.save(update_fields=VOID_UPDATE_FIELDS + ["total_amount"])

        bill.save(update_fields=VOID_UPDATE_FIELDS)
        bill.refresh_from_db()
        self.assertEqual(bill.status, "VOID")
        self.assertEqual(bill.total_amount, Decimal("900.00"))

    def test_void_bill_is_final(self):
        bill = self.make_bill(status="VOID")
        bill.void_reason = "changed my mind"
        with self.assertRaises(ValueError):
            bill.save()

    def test_bills_cannot_be_deleted(self):
        bill = self.make_bill()
        with self.assertRaises(ValueError):
            bill.delete()
        with self.assertRaises(ValueError):
            CustomerBill.objects.filter(pk=bill.pk).delete()
        with self.assertRaises(ValueError):
            CustomerBill.objects.filter(pk=bill.pk).update(total_amount=1)
        with self.assertRaises(ValueError):
            CustomerBill.objects.bulk_update([bill], ["total_amount"])
        self.assertTrue(CustomerBill.objects.filter(pk=bill.pk).exists())


class BillLineTests(DbRejects, BillingBase, TestCase):
    def setUp(self):
        self.build()
        # deliver first: a finalized bill closes its period to new deliveries
        self.delivery = self.deliver(self.cow, "2", ist(2026, 9, 5))
        self.later_delivery = self.deliver(self.cow, "1", ist(2026, 9, 6))
        self.bill =self.make_bill(status="FINALIZED", total="120")

    def line(self, **overrides):
        fields = dict(
            business_id=self.business.pk,
            bill=self.bill,
            delivery=self.delivery,
            product=self.cow,
            line_kind="COW_MILK",
            description="Cow Milk",
            unit="L",
            delivered_at=self.delivery.delivered_at,
            shift="MORNING",
            quantity=Decimal("2.000"),
            unit_price=Decimal("60.00"),
            amount=Decimal("120.00"),
        )
        fields.update(overrides)
        return CustomerBillLine(**fields)

    def test_lines_are_inserted_in_bulk_and_listed_in_time_order(self):
        later = self.later_delivery
        CustomerBillLine.objects.bulk_create([
            self.line(delivery=later, delivered_at=later.delivered_at),
            self.line(),
        ])
        lines = list(self.bill.lines.all())
        self.assertEqual([l.delivery_id for l in lines], [self.delivery.pk, later.pk])

    def test_a_delivery_appears_once_per_bill(self):
        self.line().save()
        self.assertDbRejects(self.line().save)

    def test_value_checks(self):
        self.assertDbRejects(self.line(quantity=Decimal("0")).save)
        self.assertDbRejects(self.line(unit_price=Decimal("-1")).save)
        self.assertDbRejects(self.line(amount=Decimal("-1")).save)

    def test_lines_are_immutable(self):
        line = self.line()
        line.save()
        line.amount = Decimal("1.00")
        with self.assertRaises(ValueError):
            line.save()
        with self.assertRaises(ValueError):
            line.delete()
        with self.assertRaises(ValueError):
            CustomerBillLine.objects.filter(pk=line.pk).update(amount=1)
        with self.assertRaises(ValueError):
            CustomerBillLine.objects.filter(pk=line.pk).delete()
        with self.assertRaises(ValueError):
            CustomerBillLine.objects.bulk_update([line], ["amount"])


class PaymentLedgerTests(DbRejects, BillingBase, TestCase):
    def setUp(self):
        self.build()
        self.bill = self.make_bill(status="FINALIZED", total="900")
        self.payment = self.make_payment(amount="1000")

    def allocation(self, **overrides):
        fields = dict(
            business_id=self.business.pk, payment=self.payment,
            bill=self.bill, amount=Decimal("500.00"),
        )
        fields.update(overrides)
        return CustomerPaymentAllocation.objects.create(**fields)

    def reversal(self, **overrides):
        fields = dict(
            business_id=self.business.pk, payment=self.payment,
            amount=Decimal("100.00"), reason="mistake",
            reversed_at=timezone.now(), created_by=self.owner,
        )
        fields.update(overrides)
        return CustomerPaymentReversal.objects.create(**fields)

    def test_payment_checks(self):
        self.assertDbRejects(lambda: self.make_payment(amount="0"))
        self.assertDbRejects(lambda: self.make_payment(number=self.payment.payment_number))
        # Same number in another business is fine.
        self.make_payment(customer=self.other_customer, number=self.payment.payment_number)

    def test_client_ref_is_unique_per_business_and_optional(self):
        self.make_payment(client_ref="ref-1")
        self.assertDbRejects(lambda: self.make_payment(client_ref="ref-1"))
        self.make_payment(customer=self.other_customer, client_ref="ref-1")
        # Any number of payments may have no reference.
        self.make_payment()
        self.make_payment()

    def test_payments_are_immutable(self):
        self.payment.notes = "edited"
        with self.assertRaises(ValueError):
            self.payment.save()
        with self.assertRaises(ValueError):
            self.payment.delete()
        with self.assertRaises(ValueError):
            CustomerPayment.objects.filter(pk=self.payment.pk).update(amount=1)
        with self.assertRaises(ValueError):
            CustomerPayment.objects.filter(pk=self.payment.pk).delete()
        with self.assertRaises(ValueError):
            CustomerPayment.objects.bulk_create([self.payment])

    def test_allocation_checks_and_immutability(self):
        allocation = self.allocation()
        self.assertDbRejects(self.allocation)  # same payment and bill twice
        other_bill = self.make_bill(
            status="FINALIZED", start=date(2026, 8, 1), end=date(2026, 8, 31),
        )
        self.assertDbRejects(lambda: self.allocation(bill=other_bill, amount=Decimal("0")))

        allocation.amount = Decimal("1.00")
        with self.assertRaises(ValueError):
            allocation.save()
        with self.assertRaises(ValueError):
            allocation.delete()
        with self.assertRaises(ValueError):
            CustomerPaymentAllocation.objects.filter(pk=allocation.pk).update(amount=1)

    def test_reversal_checks_and_immutability(self):
        reversal = self.reversal(client_ref="rev-1")
        self.assertDbRejects(lambda: self.reversal(amount=Decimal("0")))
        self.assertDbRejects(lambda: self.reversal(reason=""))
        self.assertDbRejects(lambda: self.reversal(client_ref="rev-1"))
        self.reversal()  # several reversals per payment are normal

        reversal.reason = "edited"
        with self.assertRaises(ValueError):
            reversal.save()
        with self.assertRaises(ValueError):
            reversal.delete()
        with self.assertRaises(ValueError):
            CustomerPaymentReversal.objects.filter(pk=reversal.pk).delete()

    def test_unwind_checks_and_immutability(self):
        allocation = self.allocation()
        reversal = self.reversal()
        unwind = CustomerPaymentAllocationReversal.objects.create(
            business_id=self.business.pk, reversal=reversal,
            allocation=allocation, amount=Decimal("50.00"),
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomerPaymentAllocationReversal.objects.create(
                business_id=self.business.pk, reversal=reversal,
                allocation=allocation, amount=Decimal("10.00"),
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomerPaymentAllocationReversal.objects.create(
                business_id=self.business.pk, reversal=self.reversal(),
                allocation=allocation, amount=Decimal("0"),
            )

        unwind.amount = Decimal("1.00")
        with self.assertRaises(ValueError):
            unwind.save()
        with self.assertRaises(ValueError):
            unwind.delete()

    def test_payment_methods_match_the_supplier_side(self):
        self.assertEqual(
            CustomerPayment.PaymentMethod.values,
            SupplierPayment.PaymentMethod.values,
        )


class TenantConsistencyTests(BillingBase, TestCase):
    """The database cannot compare businesses across tables; clean() does."""

    def setUp(self):
        self.build()

    def test_bill_customer_must_share_the_business(self):
        bill = CustomerBill(
            business=self.other_business, customer=self.customer,
            bill_number="Z-1", period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
        )
        with self.assertRaises(ValidationError):
            bill.clean()

    def test_payment_customer_must_share_the_business(self):
        payment = CustomerPayment(
            business=self.other_business, customer=self.customer,
            payment_number="Z-1", paid_at=timezone.now(), amount=Decimal("1"),
        )
        with self.assertRaises(ValidationError):
            payment.clean()

    def test_allocation_needs_one_business_and_one_customer(self):
        bill = self.make_bill(status="FINALIZED")
        payment = self.make_payment()

        foreign_bill = self.make_bill(
            customer=self.other_customer, status="FINALIZED",
        )
        with self.assertRaises(ValidationError):
            CustomerPaymentAllocation(
                business=self.business, payment=payment, bill=foreign_bill,
                amount=Decimal("1"),
            ).clean()

        same_business_other_customer = self.make_bill(
            customer=self.customer2, status="FINALIZED",
        )
        with self.assertRaises(ValidationError):
            CustomerPaymentAllocation(
                business=self.business, payment=payment,
                bill=same_business_other_customer, amount=Decimal("1"),
            ).clean()

        CustomerPaymentAllocation(
            business=self.business, payment=payment, bill=bill,
            amount=Decimal("1"),
        ).clean()

    def test_line_delivery_must_share_the_business(self):
        bill = self.make_bill(customer=self.other_customer, status="FINALIZED")
        delivery = self.deliver(self.cow, "1", ist(2026, 9, 5))
        line = CustomerBillLine(
            business=self.other_business, bill=bill, delivery=delivery,
            product=self.cow, line_kind="COW_MILK", description="x", unit="L",
            delivered_at=delivery.delivered_at, shift="MORNING",
            quantity=Decimal("1"), unit_price=Decimal("1"), amount=Decimal("1"),
        )
        with self.assertRaises(ValidationError):
            line.clean()


class BillingAdminTests(BillingBase, TestCase):
    def test_every_billing_model_is_read_only_in_admin(self):
        request = RequestFactory().get("/")
        models = (
            CustomerBill, CustomerBillLine, CustomerPayment,
            CustomerPaymentAllocation, CustomerPaymentReversal,
            CustomerPaymentAllocationReversal,
        )
        for model in models:
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))
