"""A finalized bill closes its period to deliveries; customers with history stay."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import CustomerBill
from apps.businesses.models import Business
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.deliveries.services import cancel_delivery, correct_delivery
from apps.billing.bill_services import record_opening_balance
from tests.billing.base import SEP, BillFlow, ist

UTC = ZoneInfo("UTC")


class PeriodGuardTests(BillFlow, TestCase):
    def setUp(self):
        super().setUp()
        self.september()
        self.delivery = Delivery.objects.filter(product=self.cow).order_by("delivered_at").first()
        self.bill = self.finalize(self.draft())

    def cancel(self, delivery=None):
        return cancel_delivery(
            business_id=self.business.pk, delivery_id=(delivery or self.delivery).pk,
            user=self.owner, reason="mistake",
        )

    def correct(self):
        return correct_delivery(
            business_id=self.business.pk, delivery_id=self.delivery.pk,
            user=self.owner, reason="mistake", quantity="3",
        )

    def test_a_missed_delivery_cannot_be_added_to_a_billed_period(self):
        with self.assertRaisesMessage(ValidationError, self.bill.bill_number):
            self.deliver(self.cow, "1", ist(2026, 9, 10))
        self.assertFalse(Delivery.objects.filter(delivered_at__day=10).exists())

    def test_cancel_and_correct_are_refused_and_change_nothing(self):
        with self.assertRaisesMessage(ValidationError, self.bill.bill_number):
            self.cancel()
        with self.assertRaisesMessage(ValidationError, self.bill.bill_number):
            self.correct()
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "DELIVERED")
        self.assertEqual(Delivery.objects.filter(status="CANCELLED").count(), 0)

    def test_other_customers_and_other_dates_are_free(self):
        self.deliver(self.cow, "1", ist(2026, 9, 10), customer=self.customer2)
        self.deliver(self.cow, "1", ist(2026, 8, 31, 23, 59))
        self.deliver(self.cow, "1", ist(2026, 10, 1, 0, 0))

    def test_the_edges_of_the_period(self):
        with self.assertRaises(ValidationError):
            self.deliver(self.ghee, "1", ist(2026, 9, 1, 0, 0))
        with self.assertRaises(ValidationError):
            self.deliver(self.ghee, "1", ist(2026, 9, 30, 23, 59))

    def test_voiding_the_bill_reopens_the_period(self):
        self.void(self.bill)

        self.deliver(self.ghee, "1", ist(2026, 9, 10))
        corrected = correct_delivery(
            business_id=self.business.pk, delivery_id=self.delivery.pk,
            user=self.owner, reason="mistake", quantity="3",
        )
        self.assertEqual(corrected.replaces_id, self.delivery.pk)
        self.cancel(corrected)

    def test_drafts_do_not_lock_anything(self):
        self.void(self.bill)
        self.draft()
        self.deliver(self.ghee, "1", ist(2026, 9, 10))

    def test_an_opening_balance_does_not_lock_its_day(self):
        record_opening_balance(
            business_id=self.business.pk, customer_id=self.customer2.pk,
            amount="100", as_of_date=date(2026, 8, 31), user=self.owner,
        )
        self.deliver(self.cow, "1", ist(2026, 8, 31), customer=self.customer2)

    def test_a_retry_of_an_earlier_delivery_is_still_answered(self):
        self.void(self.bill)
        first = self.deliver(self.ghee, "1", ist(2026, 9, 10), client_ref="retry-1")
        self.finalize(self.draft())

        again = self.deliver(self.ghee, "1", ist(2026, 9, 10), client_ref="retry-1")

        self.assertEqual(again.pk, first.pk)

    def test_the_business_timezone_decides_which_day_is_locked(self):
        Business.objects.filter(pk=self.business.pk).update(timezone="America/New_York")
        self.void(self.bill)
        self.deliver(self.ghee, "1", datetime(2026, 9, 1, 5, 0, tzinfo=UTC))
        self.finalize(self.draft())

        # 23:59 on 30 Sep in New York is locked; 00:00 on 1 Oct is not.
        with self.assertRaises(ValidationError):
            self.deliver(self.buffalo, "1", datetime(2026, 10, 1, 3, 59, tzinfo=UTC))
        self.deliver(self.buffalo, "1", datetime(2026, 10, 1, 4, 0, tzinfo=UTC))

    def test_staff_cannot_slip_a_delivery_into_a_billed_day(self):
        yesterday = timezone.now() - timedelta(days=1)
        day = yesterday.astimezone(ZoneInfo("Asia/Kolkata")).date()
        self.deliver(self.cow, "1", yesterday, customer=self.customer2)
        self.finalize(self.draft(period=(day, day), customer=self.customer2))

        with self.assertRaises(ValidationError):
            self.deliver(
                self.ghee, "1", yesterday, customer=self.customer2, user=self.staff,
            )


class CustomerDeleteGuardTests(BillFlow, TestCase):
    def delete(self, customer):
        client = APIClient()
        client.force_authenticate(self.owner)
        return client.delete(reverse(
            "customers:customer-detail",
            kwargs={"business_id": self.business.pk, "pk": customer.pk},
        ))

    def test_customers_with_history_cannot_be_deleted(self):
        delivered = Customer.objects.create(business=self.business, customer_code="D1", name="D1")
        self.deliver(self.cow, "1", ist(2026, 9, 5), customer=delivered)
        billed = Customer.objects.create(business=self.business, customer_code="D2", name="D2")
        self.make_bill(customer=billed)
        paid = Customer.objects.create(business=self.business, customer_code="D3", name="D3")
        self.make_payment(customer=paid)

        for customer in (delivered, billed, paid):
            with self.subTest(customer=customer.customer_code):
                self.assertEqual(self.delete(customer).status_code, 400)
                customer.refresh_from_db()
                self.assertFalse(customer.is_deleted)

    def test_customers_without_history_can_be_deleted(self):
        fresh = Customer.objects.create(business=self.business, customer_code="D4", name="D4")
        cancelled = Customer.objects.create(business=self.business, customer_code="D5", name="D5")
        gone = self.deliver(self.cow, "1", ist(2026, 9, 5), customer=cancelled)
        cancel_delivery(
            business_id=self.business.pk, delivery_id=gone.pk,
            user=self.owner, reason="mistake",
        )

        self.assertEqual(self.delete(fresh).status_code, 204)
        self.assertEqual(self.delete(cancelled).status_code, 204)


class DeliveryAdminTests(BillFlow, TestCase):
    def test_deliveries_are_read_only_in_admin(self):
        request = RequestFactory().get("/")
        model_admin = admin.site._registry[Delivery]
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))
