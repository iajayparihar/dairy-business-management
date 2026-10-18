"""Profit estimate: hand-computed for September 2026 (Sep 1 is a Tuesday)."""
from datetime import date

from django.urls import reverse

from apps.expenses import payroll
from apps.products.models import ProductPrice
from tests.reports.base import D, PRICE_FROM, ReportBase, ist


class ProfitTests(ReportBase):
    def setUp(self):
        super().setUp()
        ProductPrice.objects.create(
            business=self.business, product=self.cow, price_type="PURCHASE",
            unit_price=D("8.00"), effective_from=PRICE_FROM)
        self.milk(self.s1, 100, ist(2026, 9, 3, 6))            # cost 1000, +100 L
        self.deliver(self.cow, "5", ist(2026, 9, 4, 7))        # 300, -5 L
        self.deliver(self.ghee, "2", ist(2026, 9, 5, 8))       # 1600
        self.sell(mode="CASH", user=self.owner, sold_at=ist(2026, 9, 6, 10))  # 400
        self.spend("150.00", date(2026, 9, 7))
        self.deliver(self.cow, "1", ist(2026, 10, 2, 7))       # 60, October

    def get(self, user=None, **params):
        url = reverse("reports:profit", kwargs={"business_id": self.business.pk})
        return self.api(user or self.owner).get(url, params)

    def test_month_known_figures(self):
        r = self.get(period="month", **{"from": "2026-09-01", "to": "2026-09-30"})
        self.assertEqual(r.status_code, 200, r.content)
        b = r.json()
        self.assertEqual(len(b["rows"]), 1)
        row = b["rows"][0]
        self.assertEqual(row["sales"], "2300.00")
        self.assertEqual(row["purchases"], "1000.00")
        self.assertEqual(row["expenses"], "150.00")
        self.assertEqual(row["salary"], "0.00")
        self.assertEqual(row["profit"], "1150.00")
        # 95 L left at Rs.8 = 760 -> only 240 of the purchase was "used".
        self.assertEqual(row["inventory_change"], "760.00")
        self.assertEqual(row["profit_stock_adjusted"], "1910.00")
        self.assertEqual(b["totals"]["profit"], "1150.00")

    def test_two_months_split_correctly(self):
        b = self.get(period="month", **{"from": "2026-09-15", "to": "2026-10-10"}).json()
        self.assertEqual([r["from"] for r in b["rows"]], ["2026-09-01", "2026-10-01"])
        self.assertEqual(b["rows"][1]["sales"], "60.00")
        self.assertEqual(b["rows"][1]["purchases"], "0.00")

    def test_weeks_snap_to_monday_and_exclude_salary(self):
        b = self.get(period="week", **{"from": "2026-09-01", "to": "2026-09-10"}).json()
        self.assertEqual(b["from"], "2026-08-31")
        self.assertEqual(b["to"], "2026-09-13")
        w1, w2 = b["rows"]
        self.assertEqual((w1["sales"], w1["purchases"], w1["expenses"]),
                         ("2300.00", "1000.00", "0.00"))
        self.assertEqual((w2["sales"], w2["expenses"]), ("0.00", "150.00"))
        self.assertFalse(w1["salary_included"])
        self.assertIsNone(w1["salary"])

    def test_salary_counts_in_its_month(self):
        emp = payroll.create_employee(
            business_id=self.business.pk, user=self.owner, name="Ramesh",
            monthly_salary="12000.00", joined_on=date(2025, 1, 1))
        payroll.generate_salary(
            business_id=self.business.pk, user=self.owner, month=date(2026, 9, 1))
        row = self.get(period="month", **{"from": "2026-09-01", "to": "2026-09-30"}
                       ).json()["rows"][0]
        self.assertEqual(row["salary"], "12000.00")
        self.assertEqual(row["profit"], "-10850.00")

    def test_unvalued_stock_is_flagged_not_zeroed(self):
        self.stock_in(self.paneer, 4, ist(2026, 9, 2))
        b = self.get(period="month", **{"from": "2026-09-01", "to": "2026-09-30"}).json()
        self.assertEqual(b["unvalued_products"], 1)
        self.assertEqual(b["rows"][0]["inventory_change"], "760.00")

    def test_cancelled_sale_not_counted(self):
        from apps.sales.services import cancel_sale
        sale = self.sell(mode="CASH", user=self.owner, sold_at=ist(2026, 9, 8, 10),
                         lines=[{"product_id": self.ghee.pk, "quantity": "1"}])
        before = self.get(period="month", **{"from": "2026-09-01", "to": "2026-09-30"}).json()
        self.assertEqual(before["rows"][0]["sales"], "3100.00")
        cancel_sale(business_id=self.business.pk, sale_id=sale.pk,
                    user=self.owner, reason="mistake")
        after = self.get(period="month", **{"from": "2026-09-01", "to": "2026-09-30"}).json()
        self.assertEqual(after["rows"][0]["sales"], "2300.00")

    def test_validation_and_access(self):
        q = {"from": "2026-09-01", "to": "2026-09-30"}
        self.assertEqual(self.get(self.staff, **q).status_code, 403)
        self.assertEqual(self.get(self.manager, **q).status_code, 200)
        self.assertEqual(self.get(period="year", **q).status_code, 400)
        self.assertEqual(self.get(**{"from": "2026-09-30", "to": "2026-09-01"}).status_code, 400)
        self.assertEqual(self.get(**{"from": "2024-01-01", "to": "2026-09-01"}).status_code, 400)
        self.assertEqual(self.get(**{"from": "2026-09-01"}).status_code, 400)
        self.assertEqual(self.get(bogus="1", **q).status_code, 400)

    def test_manager_does_not_see_salary(self):
        payroll.create_employee(
            business_id=self.business.pk, user=self.owner, name="Ramesh",
            monthly_salary="12000.00", joined_on=date(2025, 1, 1))
        payroll.generate_salary(
            business_id=self.business.pk, user=self.owner, month=date(2026, 9, 1))
        q = {"period": "month", "from": "2026-09-01", "to": "2026-09-30"}
        row = self.get(self.manager, **q).json()["rows"][0]
        self.assertIsNone(row["salary"])
        self.assertFalse(row["salary_included"])
        self.assertEqual(row["profit"], "1150.00")

    def test_csv_and_pdf_export(self):
        import csv, io
        q = {"period": "month", "from": "2026-09-01", "to": "2026-09-30"}
        r = self.get(export="csv", **q)
        text = b"".join(r.streaming_content).decode().lstrip("﻿")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[1][2], "2300.00")          # sales
        self.assertEqual(rows[1][7], "1150.00")          # profit
        self.assertEqual(rows[-1][0], "TOTAL")
        self.assertTrue(self.get(export="pdf", **q).content.startswith(b"%PDF"))
        self.assertEqual(self.get(export="zip", **q).status_code, 400)
