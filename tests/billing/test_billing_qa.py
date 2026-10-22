"""QA sweep: customer bills and payments, gaps beyond tests/billing/test_api.py."""
from datetime import date
from uuid import uuid4

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.billing.models import CustomerBill, CustomerPayment
from tests.billing.test_api import ApiBase

FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


@FAST
class BillingQA(ApiBase, TestCase):
    def test_payment_text_fields_have_limits(self):
        for kw in ({"reference_number": "x" * 101}, {"notes": "x" * 5001}, {"client_ref": "x" * 65}):
            r = self.post_payment({"amount": "10", **kw})
            self.assertEqual(r.status_code, 400, str(kw)[:60])

    def test_amount_limits_and_types(self):
        for amt in ("0", "-1", "abc", "NaN", "Infinity", "1" * 15, None, [1], {"a": 1}, "1.005", True):
            r = self.post_payment({"amount": amt})
            self.assertEqual(r.status_code, 400, amt)
        self.assertEqual(self.post_payment({"amount": "99999999999.99"}).status_code, 201)

    def test_payment_response_money_is_string(self):
        r = self.post_payment({"amount": "10.5"})
        for key in ("amount", "advance_remaining"):
            self.assertIsInstance(r.json()[key], str, key)

    def test_reverse_and_allocate_validation(self):
        self.make_bill(status="FINALIZED", total="100")
        pid = self.post_payment({"amount": "100"}).json()["id"]
        rev = self.url("payment-reverse", payment_id=pid)
        for body in ({}, {"reason": ""}, {"reason": "  "}, {"reason": "x" * 501}, {"amount": "0", "reason": "r"},
                     {"amount": "101", "reason": "r"}, {"reason": "r", "bogus": 1}):
            self.assertEqual(self.api().post(rev, body, format="json").status_code, 400, str(body)[:60])
        al = self.url("payment-allocate", payment_id=pid)
        for body in ({}, {"bill_id": "x", "amount": "1"}, {"bill_id": str(uuid4()), "amount": "1"}, {"bill_id": str(uuid4())}):
            self.assertIn(self.api().post(al, body, format="json").status_code, (400, 404), body)

    def test_payment_list_paginated_and_other_customers_isolated(self):
        for i in range(3):
            self.post_payment({"amount": "5"})
        self.post_payment({"amount": "7"}, customer=self.customer2)
        r = self.api().get(self.cust_url("payment-list-create"), {"page_size": 2}).json()
        self.assertEqual((r["count"], len(r["results"])), (3, 2))
        self.assertLess(self.api().get(self.cust_url("payment-list-create"), {"page": "x", "page_size": "y"}).status_code, 500)

    def test_bill_list_filters_and_perf(self):
        for m in range(1, 6):
            self.make_bill(start=date(2026, m, 1), end=date(2026, m, 28), status="FINALIZED", total="10")
        for q in ({"status": "X"}, {"customer": "x"}, {"start_date": "x"}, {"bill_type": "X"}):
            self.assertEqual(self.api().get(self.url("bill-list"), q).status_code, 400, q)
        with CaptureQueriesContext(connection) as ctx:
            r = self.api().get(self.url("bill-list"), {"page_size": 5})
        self.assertEqual(r.json()["count"], 5)
        self.assertLess(len(ctx), 15)

    def test_generate_and_void_validation(self):
        gen = self.url("bill-generate")
        for body in ({}, {"period_start": "x", "period_end": "y"}, {"period_start": "2026-09-30", "period_end": "2026-09-01"},
                     {"period_start": "2026-09-01", "period_end": "2026-09-30", "bogus": 1},
                     {"period_start": "2026-09-01", "period_end": "2099-09-30"}):
            self.assertEqual(self.api().post(gen, body, format="json").status_code, 400, body)
        bill = self.make_bill(status="FINALIZED", total="50")
        void = self.url("bill-void", bill_id=bill.pk)
        for body in ({}, {"reason": ""}, {"reason": "x" * 501}, {"reason": "r", "bogus": 1}):
            self.assertEqual(self.api().post(void, body, format="json").status_code, 400, str(body)[:60])

    def test_pdf_params(self):
        bill = self.make_bill(status="FINALIZED", total="50")
        u = self.url("bill-pdf", bill_id=bill.pk)  # noqa
        self.assertEqual(self.api().get(u, {"lang": "fr"}).status_code, 400)

    def test_balance_and_statement_money_is_string(self):
        self.make_bill(status="FINALIZED", total="100")
        raw = self.api().get(self.cust_url("customer-balance")).content.decode()
        self.assertNotRegex(raw, r':\s*-?\d+\.\d+[,}]')
