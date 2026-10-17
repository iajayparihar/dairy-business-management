"""Bill PDF: from the stored snapshot only, byte-identical every time."""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.billing.pdf import render_bill_pdf
from apps.customers.models import Customer
from tests.billing.base import BillFlow


class PdfTests(BillFlow, TestCase):
    def finalized(self):
        self.september()
        return self.finalize(self.draft())

    def test_pdf_is_a_valid_document_and_byte_identical_on_every_render(self):
        bill = self.finalized()

        first = render_bill_pdf(bill)
        second = render_bill_pdf(bill)

        self.assertTrue(first.startswith(b"%PDF"))
        self.assertGreater(len(first), 1000)
        self.assertEqual(first, second)

    def test_editing_the_customer_later_does_not_change_the_printed_bill(self):
        bill = self.finalized()
        before = render_bill_pdf(bill)

        Customer.objects.filter(pk=self.customer.pk).update(
            name="Someone Else", address="Elsewhere",
        )

        self.assertEqual(render_bill_pdf(bill), before)

    def test_void_bill_prints_differently(self):
        bill = self.finalized()
        original = render_bill_pdf(bill)

        voided, _ = self.void(bill)

        self.assertNotEqual(render_bill_pdf(voided), original)

    def test_opening_balance_has_a_pdf_too(self):
        from datetime import date
        from apps.billing.bill_services import record_opening_balance

        bill = record_opening_balance(
            business_id=self.business.pk, customer_id=self.customer.pk,
            amount="500", as_of_date=date(2026, 8, 31), user=self.owner,
        ).bill

        self.assertTrue(render_bill_pdf(bill).startswith(b"%PDF"))

    def test_a_draft_has_no_pdf(self):
        self.september()
        with self.assertRaises(ValueError):
            render_bill_pdf(self.draft())


class PdfApiTests(BillFlow, TestCase):
    def get(self, bill, user=None):
        client = APIClient()
        client.force_authenticate(user or self.owner)
        return client.get(reverse(
            "billing:bill-pdf",
            kwargs={"business_id": self.business.pk, "bill_id": bill.pk},
        ))

    def test_endpoint(self):
        self.september()
        draft = self.draft()
        self.assertEqual(self.get(draft).status_code, 400)

        bill = self.finalize(draft)
        response = self.get(bill)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(bill.bill_number, response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertEqual(self.get(bill, user=self.staff).status_code, 403)
