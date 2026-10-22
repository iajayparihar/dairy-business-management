"""QA sweep across every report endpoint."""
import re

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from tests.reports.test_tables import TableBase, ist

FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])

# endpoint name -> minimal valid params
ENDPOINTS = {
    "milk-balance": {"date": "2026-09-05"},
    "closing-stock": {"month": "2026-09"},
    "daily-summary": {},
    "profit": {},
    "supplier-balances": {},
    "customer-outstanding": {},
    "stock": {},
    "product-sales": {},
    "expenses": {},
}
GARBAGE = ["x", "", "0000-00-00", "2026-13-45", "9999-12-31", "-1", "1e9", "%", "' OR 1=1", "a" * 5000, "NaN", "\x00"]


@FAST
class ReportsQA(TableBase):
    def setUp(self):
        super().setUp()
        self.milk(self.s1, 100, ist(2026, 9, 3))
        self.spay(self.s1, 600, ist(2026, 9, 10), auto_allocate=True)
        self.deliver(self.cow, "2", ist(2026, 9, 5))
        self.spend("250.00", ist(2026, 9, 6).date())

    def test_every_endpoint_blocks_staff_and_outsiders_and_anonymous(self):
        from rest_framework.test import APIClient
        for name, params in ENDPOINTS.items():
            self.assertEqual(self.get(name, user=self.staff, **params).status_code, 403, name)
            self.assertEqual(self.get(name, user=self.outsider, **params).status_code, 403, name)
            from django.urls import reverse
            url = reverse(f"reports:{name}", kwargs={"business_id": self.business.pk})
            self.assertEqual(APIClient().get(url, params).status_code, 401, name)

    def test_default_params_are_ok_and_money_is_never_a_float(self):
        for name, params in ENDPOINTS.items():
            r = self.get(name, **params)
            self.assertEqual(r.status_code, 200, (name, r.content[:160]))
            raw = r.content.decode()
            bad = re.findall(r'"[a-z_]*(?:amount|total|balance|price|cost|profit|sales|outstanding|paid|billed|value)[a-z_]*":\s*-?\d+\.\d+', raw)
            self.assertEqual(bad, [], (name, bad[:3]))

    def test_garbage_in_any_param_is_never_500(self):
        for name, params in ENDPOINTS.items():
            for key in list(params) + ["from", "to", "date", "month", "as_of", "period", "page", "page_size", "export", "supplier_id", "product_id", "only_due", "ordering", "compare", "unknown"]:
                for value in GARBAGE:
                    r = self.get(name, **{**params, key: value})
                    self.assertLess(r.status_code, 500, (name, key, value[:20]))

    def test_unknown_query_param_is_rejected_consistently(self):
        """A typo such as ?mnth=2026-09 must not silently return the wrong report."""
        for name, params in ENDPOINTS.items():
            r = self.get(name, **{**params, "bogus_param": "1"})
            self.assertEqual(r.status_code, 400, name)

    def test_exports_for_every_table_report(self):
        for name in ("supplier-balances", "customer-outstanding", "stock", "product-sales", "expenses"):
            for kind, magic in (("csv", b"\xef\xbb\xbf"), ("pdf", b"%PDF")):
                r = self.get(name, export=kind)
                self.assertEqual(r.status_code, 200, (name, kind))
                body = b"".join(r.streaming_content) if r.streaming else r.content
                self.assertTrue(body.startswith(magic), (name, kind))
        self.assertEqual(self.get("supplier-balances", export="xlsx").status_code, 400)

    def test_page_params_on_table_reports(self):
        for name in ("supplier-balances", "customer-outstanding", "stock", "product-sales", "expenses"):
            r = self.get(name, page_size="1")
            self.assertEqual(r.status_code, 200, name)

    def test_query_counts_stay_flat(self):
        for name, params in ENDPOINTS.items():
            with CaptureQueriesContext(connection) as ctx:
                self.get(name, **params)
            self.assertLess(len(ctx), 60, (name, len(ctx)))

    def test_salary_hidden_from_manager(self):
        r = self.get("profit", user=self.manager)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("salary_cost", r.content.decode().replace("salary_visible", ""))
