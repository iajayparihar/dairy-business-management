"""Phase 4 Step 4: paginated statement, print view, money-data access."""
from datetime import datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.payments.selectors import get_supplier_balance
from apps.suppliers.models import Supplier
from tests.payment.test_payment_hardening import IST, User
from tests.payment.test_payment_reversals import ReversalMixin

D = Decimal


class StatementApiTests(ReversalMixin, TestCase):
    def setUp(self):
        self.build()
        self.staff = User.objects.create_user("stf", password="pw-123456")
        self.manager = User.objects.create_user("mgr", password="pw-123456")
        for user, role in ((self.staff, "STAFF"), (self.manager, "MANAGER")):
            BusinessMembership.objects.create(
                business=self.business, user=user, role=role, is_active=True
            )
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.base = f"/api/v1/businesses/{self.business.id}/suppliers/{self.supplier.id}"

    def ago(self, days):
        return timezone.now() - timedelta(days=days)

    def seed(self, n=6):
        for i in range(n):
            self.purchase(f"P{i}", "100.00", self.ago(10 - i))
        pay = self.pay(number="PAY1", amount="250.00", paid_at=self.ago(2), auto_allocate=True)
        self.reverse(pay, "50.00", reason="part")

    def test_pages_line_up_and_last_balance_is_closing(self):
        self.seed()
        full = self.client.get(f"{self.base}/statement/?page_size=200").data
        self.assertEqual(full["pagination"]["count"], 8)

        rows, page = [], 1
        while True:
            r = self.client.get(f"{self.base}/statement/?page_size=3&page={page}")
            self.assertEqual(r.status_code, 200)
            rows += r.data["transactions"]
            # Period totals are the same on every page.
            self.assertEqual(r.data["closing_balance"], full["closing_balance"])
            self.assertEqual(r.data["opening_balance"], full["opening_balance"])
            if page == r.data["pagination"]["num_pages"]:
                break
            page += 1

        self.assertEqual(page, 3)
        self.assertEqual(
            [(t["reference"], t["balance"]) for t in rows],
            [(t["reference"], t["balance"]) for t in full["transactions"]],
        )
        self.assertEqual(rows[-1]["balance"], full["closing_balance"])
        bal = get_supplier_balance(
            business_id=self.business.id, supplier_id=self.supplier.id
        )
        self.assertEqual(
            full["closing_balance"],
            bal["outstanding_amount"] - bal["available_credit"],
        )

    def test_identity_fields(self):
        r = self.client.get(f"{self.base}/statement/")
        for key in ("business_name", "currency", "timezone", "supplier_code",
                    "supplier_name", "generated_at"):
            self.assertIn(key, r.data)
        self.assertEqual(r.data["currency"], "INR")

    def test_print_view_is_escaped_local_time_and_no_store(self):
        Supplier.objects.filter(pk=self.supplier.pk).update(
            name="<script>alert(1)</script>"
        )
        self.purchase("P", "100.00", datetime(2026, 10, 10, 9, 0, tzinfo=IST))
        pay = self.pay(
            number="PAY-X", amount="40.00",
            paid_at=datetime(2026, 10, 16, 0, 30, tzinfo=IST),
        )
        self.reverse(pay, "10.00", reason="<b>oops</b>")

        r = self.client.get(
            f"{self.base}/statement/print/?start_date=2026-10-01&end_date=2026-10-31"
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Type"].startswith("text/html"))
        self.assertEqual(r["Cache-Control"], "no-store")
        html = r.content.decode()
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&lt;b&gt;oops&lt;/b&gt;", html)
        self.assertIn("16 Oct 2026 00:30", html)  # business-local, not UTC
        self.assertIn("PAY-X", html)
        self.assertIn("Closing balance", html)
        self.assertIn("INR", html)
        # 100 - 40 + 10 reversed back
        self.assertIn("70.00", html)

    def test_print_defaults_and_limits(self):
        self.assertEqual(self.client.get(f"{self.base}/statement/print/").status_code, 200)
        for q in ("?start_date=2025-01-01&end_date=2026-10-01",
                  "?start_date=2026-10-05&end_date=2026-10-01",
                  "?start_date=nope&end_date=2026-10-01"):
            self.assertEqual(
                self.client.get(f"{self.base}/statement/print/{q}").status_code, 400, q)
        self.assertEqual(
            self.client.get(f"{self.base}/statement/?start_date=bad").status_code, 400)

    def test_money_endpoints_are_owner_manager_only(self):
        urls = [f"{self.base}/statement/", f"{self.base}/statement/print/",
                f"{self.base}/payments/"]
        staff, outsider, mgr = APIClient(), APIClient(), APIClient()
        staff.force_authenticate(self.staff)
        outsider.force_authenticate(self.other_owner)
        mgr.force_authenticate(self.manager)
        for url in urls:
            self.assertEqual(staff.get(url).status_code, 403, url)
            self.assertEqual(outsider.get(url).status_code, 403, url)
            self.assertEqual(mgr.get(url).status_code, 200, url)

        body = {"payment_number": "M1", "paid_at": "2026-10-15T10:00:00+05:30", "amount": "5.00"}
        self.assertEqual(staff.post(f"{self.base}/payments/", body, format="json").status_code, 403)
        self.assertEqual(mgr.post(f"{self.base}/payments/", body, format="json").status_code, 201)

    def test_foreign_supplier_is_404(self):
        foreign = Supplier.objects.create(business=self.other, supplier_code="F", name="F")
        url = f"/api/v1/businesses/{self.business.id}/suppliers/{foreign.id}"
        for tail in ("/statement/", "/statement/print/", "/payments/"):
            self.assertEqual(self.client.get(url + tail).status_code, 404, tail)

    def test_payment_list_is_paginated(self):
        for i in range(3):
            self.pay(number=f"L{i}", amount="1.00")
        r = self.client.get(f"{self.base}/payments/?page_size=2")
        self.assertEqual(r.data["count"], 3)
        self.assertEqual(len(r.data["results"]), 2)
        self.assertIsNotNone(r.data["next"])
