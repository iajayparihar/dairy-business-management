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


class HindiPdfTests(BillFlow, TestCase):
    """Hindi labels and Devanagari names are shaped into images (reportlab cannot)."""

    def finalized(self):
        self.september()
        return self.finalize(self.draft())

    def images(self, pdf):
        return pdf.count(b"/Subtype /Image")

    def test_hindi_bill_is_valid_stable_and_differs_from_english(self):
        bill = self.finalized()

        hindi = render_bill_pdf(bill, "hi")

        self.assertTrue(hindi.startswith(b"%PDF"))
        self.assertEqual(hindi, render_bill_pdf(bill, "hi"))
        self.assertNotEqual(hindi, render_bill_pdf(bill, "en"))
        self.assertGreater(self.images(hindi), 0)
        self.assertEqual(self.images(render_bill_pdf(bill, "en")), 0)

    def test_business_default_language_is_used_when_none_is_given(self):
        bill = self.finalized()
        self.business.bill_language = "hi"
        self.business.save(update_fields=["bill_language"])
        bill.refresh_from_db()

        self.assertEqual(render_bill_pdf(bill), render_bill_pdf(bill, "hi"))

    def test_devanagari_customer_name_is_shaped_on_the_english_bill(self):
        Customer.objects.filter(pk=self.customer.pk).update(name="रामू यादव")
        bill = self.finalized()

        self.assertGreater(self.images(render_bill_pdf(bill, "en")), 0)

    def test_void_bill_in_hindi(self):
        bill = self.finalized()
        original = render_bill_pdf(bill, "hi")
        voided, _ = self.void(bill)

        void_pdf = render_bill_pdf(voided, "hi")

        self.assertTrue(void_pdf.startswith(b"%PDF"))
        self.assertNotEqual(void_pdf, original)

    def test_opening_balance_in_hindi(self):
        from datetime import date
        from apps.billing.bill_services import record_opening_balance

        bill = record_opening_balance(
            business_id=self.business.pk, customer_id=self.customer.pk,
            amount="500", as_of_date=date(2026, 8, 31), user=self.owner,
        ).bill

        self.assertTrue(render_bill_pdf(bill, "hi").startswith(b"%PDF"))

    def test_unknown_language_is_refused(self):
        with self.assertRaises(ValueError):
            render_bill_pdf(self.finalized(), "xx")


class HindiPdfApiTests(BillFlow, TestCase):
    def get(self, bill, lang=None, user=None):
        client = APIClient()
        client.force_authenticate(user or self.owner)
        url = reverse(
            "billing:bill-pdf",
            kwargs={"business_id": self.business.pk, "bill_id": bill.pk},
        )
        return client.get(url, {"lang": lang} if lang is not None else {})

    def test_lang_param(self):
        self.september()
        bill = self.finalize(self.draft())

        hindi = self.get(bill, "hi")
        self.assertEqual(hindi.status_code, 200)
        self.assertEqual(hindi.content, render_bill_pdf(bill, "hi"))
        self.assertEqual(self.get(bill, "en").content, render_bill_pdf(bill, "en"))
        self.assertEqual(self.get(bill).content, render_bill_pdf(bill, "en"))

        bad = self.get(bill, "xx")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("lang", bad.data)
        self.assertEqual(self.get(bill, "hi", user=self.staff).status_code, 403)

    def test_business_default_is_used_and_lang_overrides_it(self):
        self.september()
        bill = self.finalize(self.draft())
        self.business.bill_language = "hi"
        self.business.save(update_fields=["bill_language"])

        self.assertEqual(self.get(bill).content, render_bill_pdf(bill, "hi"))
        self.assertEqual(self.get(bill, "en").content, render_bill_pdf(bill, "en"))
