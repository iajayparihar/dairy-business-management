from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.deliveries.models import Delivery
from tests.billing.base import BillFlow


class AuditBillsTests(BillFlow, TestCase):
    def run_audit(self):
        out = StringIO()
        call_command("audit_bills", stdout=out)
        return out.getvalue()

    def test_clean_books_pass(self):
        self.september()
        self.finalize(self.draft())
        self.pay("500")
        self.assertIn("consistent", self.run_audit())

    def test_a_tampered_delivery_is_reported(self):
        self.september()
        self.finalize(self.draft())
        # Bypassing the services (the admin is read-only, queryset update is not).
        Delivery.objects.filter(product=self.cow).update(line_total=1)

        with self.assertRaises(CommandError):
            self.run_audit()

    def test_a_delivery_added_behind_the_guard_is_reported(self):
        self.september()
        self.finalize(self.draft())
        from tests.billing.base import ist
        from apps.deliveries.models import Delivery as D
        D.objects.create(
            business=self.business, customer=self.customer, product=self.ghee,
            delivered_at=ist(2026, 9, 20), quantity=Decimal("1"), unit_price=Decimal("800"),
        )

        with self.assertRaises(CommandError):
            self.run_audit()
