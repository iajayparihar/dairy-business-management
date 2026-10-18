"""Table reports + CSV export. Expected values are written out by hand."""
import csv
import io
from datetime import date

from django.urls import reverse

from apps.billing.statements import get_customer_balance
from apps.customers.models import Customer
from apps.inventory.services import stock_balance
from tests.reports.base import D, ReportBase, ist


class TableBase(ReportBase):
    def get(self, name, user=None, **params):
        url = reverse(f"reports:{name}", kwargs={"business_id": self.business.pk})
        return self.api(user or self.owner).get(url, params)

    def rows(self, name, **params):
        r = self.get(name, **params)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def csv_rows(self, name, **params):
        r = self.get(name, export="csv", **params)
        self.assertEqual(r.status_code, 200)
        text = b"".join(r.streaming_content).decode("utf-8")
        self.assertTrue(text.startswith("﻿"))
        return r, list(csv.reader(io.StringIO(text.lstrip("﻿"))))


class SupplierReportTests(TableBase):
    def setUp(self):
        super().setUp()
        self.milk(self.s1, 100, ist(2026, 9, 3))            # 1000
        self.spay(self.s1, 600, ist(2026, 9, 10), auto_allocate=True)
        self.milk(self.s2, 20, ist(2026, 9, 4))             # 200

    def test_balances(self):
        b = self.rows("supplier-balances")
        by = {r["code"]: r for r in b["rows"]}
        self.assertEqual(by["S1"]["outstanding"], "400.00")
        self.assertEqual(by["S1"]["payments"], "600.00")
        self.assertEqual(by["S2"]["outstanding"], "200.00")
        self.assertEqual(b["totals"]["outstanding"], "600.00")

    def test_balances_as_of_and_only_due(self):
        b = self.rows("supplier-balances", as_of="2026-09-05")
        by = {r["code"]: r for r in b["rows"]}
        self.assertEqual(by["S1"]["outstanding"], "1000.00")
        self.spay(self.s2, 200, ist(2026, 9, 12), auto_allocate=True)
        due = self.rows("supplier-balances", only_due="true")
        self.assertEqual([r["code"] for r in due["rows"]], ["S1"])

    def test_ledger_opening_events_closing(self):
        b = self.rows("supplier-ledger", supplier_id=str(self.s1.pk),
                      **{"from": "2026-09-05", "to": "2026-09-30"})
        self.assertEqual(b["meta"]["opening_balance"], "1000.00")
        self.assertEqual([r["type"] for r in b["rows"]], ["PAYMENT"])
        self.assertEqual(b["rows"][0]["balance"], "400.00")
        self.assertEqual(b["meta"]["closing_balance"], "400.00")

    def test_ledger_other_business_supplier_is_404(self):
        from apps.suppliers.models import Supplier
        foreign = Supplier.objects.create(
            business=self.other_business, supplier_code="F", name="F")
        r = self.get("supplier-ledger", supplier_id=str(foreign.pk))
        self.assertEqual(r.status_code, 404)

    def test_ledger_needs_supplier(self):
        self.assertEqual(self.get("supplier-ledger").status_code, 400)
        self.assertEqual(self.get("supplier-ledger", supplier_id="x").status_code, 400)


class CustomerOutstandingTests(TableBase):
    def setUp(self):
        super().setUp()
        self.september()                                   # 1240 for Ravi
        self.finalize(self.draft())
        self.pay("300.00", paid_at=ist(2026, 10, 2))
        self.deliver(self.cow, "1", ist(2026, 10, 3), customer=self.customer2)  # unbilled 60

    def test_rows_match_single_customer_balance(self):
        b = self.rows("customer-outstanding")
        by = {r["code"]: r for r in b["rows"]}
        self.assertEqual(by["C001"]["billed"], "1240.00")
        self.assertEqual(by["C001"]["outstanding"], "940.00")
        self.assertEqual(by["C001"]["last_payment"][:10], "2026-10-02")
        self.assertEqual(by["C002"]["billed"], "0.00")
        self.assertEqual(by["C002"]["unbilled"], "60.00")
        for c in Customer.objects.filter(business=self.business):
            single = get_customer_balance(business_id=self.business.pk, customer_id=c.pk)
            self.assertEqual(by[c.customer_code]["unbilled"], f"{single['unbilled_amount']:.2f}")
            self.assertEqual(by[c.customer_code]["net_balance"], f"{single['net_balance']:.2f}")
        self.assertEqual(b["rows"][0]["code"], "C001")  # largest outstanding first

    def test_only_due_and_type_filter(self):
        b = self.rows("customer-outstanding", only_due="true")
        self.assertEqual([r["code"] for r in b["rows"]], ["C001"])
        self.assertEqual(self.rows("customer-outstanding", customer_type="SHOP")["rows"], [])

    def test_bad_params(self):
        self.assertEqual(self.get("customer-outstanding", customer_type="X").status_code, 400)
        self.assertEqual(self.get("customer-outstanding", ordering="x").status_code, 400)
        self.assertEqual(self.get("customer-outstanding", only_due="maybe").status_code, 400)


class StockReportTests(TableBase):
    def test_identity_opening_plus_flow_equals_closing(self):
        self.milk(self.s1, 100, ist(2026, 9, 3))
        self.deliver(self.cow, "5", ist(2026, 9, 4))
        self.stock_in(self.paneer, 10, ist(2026, 8, 20))
        self.stock_in(self.paneer, 3, ist(2026, 9, 6), kind="WASTAGE")
        b = self.rows("stock", **{"from": "2026-09-01", "to": "2026-09-30"})
        by = {r["sku"]: r for r in b["rows"]}
        self.assertEqual(by["COW"]["opening"], "0.000")
        self.assertEqual(by["COW"]["bought"], "100.000")
        self.assertEqual(by["COW"]["delivered"], "5.000")
        self.assertEqual(by["COW"]["closing"], "95.000")
        self.assertEqual(by["PNR"]["opening"], "10.000")
        self.assertEqual(by["PNR"]["wastage"], "3.000")
        self.assertEqual(by["PNR"]["closing"], "7.000")
        for sku, p in (("COW", self.cow), ("PNR", self.paneer)):
            self.assertEqual(by[sku]["closing"],
                             f"{stock_balance(business_id=self.business.pk, product_id=p.pk):.3f}")

    def test_negative_stock_is_flagged(self):
        self.deliver(self.cow, "5", ist(2026, 9, 4))
        b = self.rows("stock", **{"from": "2026-09-01", "to": "2026-09-30"})
        self.assertEqual(b["rows"][0]["is_negative"], "YES")
        self.assertEqual(
            len(self.rows("stock", only_flagged="true", **{"from": "2026-09-01", "to": "2026-09-30"})["rows"]), 1)

    def test_range_cap(self):
        self.assertEqual(
            self.get("stock", **{"from": "2026-01-01", "to": "2026-09-30"}).status_code, 400)


class ProductSalesTests(TableBase):
    def setUp(self):
        super().setUp()
        self.deliver(self.cow, "5", ist(2026, 9, 4))                      # 300
        self.sell(mode="CASH", user=self.owner, sold_at=ist(2026, 9, 5, 10),
                  lines=[{"product_id": self.cow.pk, "quantity": "3"}])   # 180
        self.sell(mode="CASH", user=self.owner, sold_at=ist(2026, 9, 5, 11))  # ghee 0.5 = 400

    def test_by_product(self):
        b = self.rows("product-sales", **{"from": "2026-09-01", "to": "2026-09-30"})
        by = {r["sku"]: r for r in b["rows"]}
        self.assertEqual(by["COW"]["qty_total"], "8.000")
        self.assertEqual(by["COW"]["amount_total"], "480.00")
        self.assertEqual(by["COW"]["avg_price"], "60.00")
        self.assertEqual(by["GHEE"]["amount_shop"], "400.00")
        self.assertEqual(b["totals"]["amount_total"], "880.00")

    def test_by_day_and_filter(self):
        b = self.rows("product-sales", group_by="day", product_id=str(self.cow.pk),
                      **{"from": "2026-09-01", "to": "2026-09-30"})
        self.assertEqual([(r["date"], r["amount_total"]) for r in b["rows"]],
                         [("2026-09-04", "300.00"), ("2026-09-05", "180.00")])

    def test_cancelled_sale_excluded(self):
        from apps.sales.services import cancel_sale
        s = self.sell(mode="CASH", user=self.owner, sold_at=ist(2026, 9, 6, 10))
        cancel_sale(business_id=self.business.pk, sale_id=s.pk, user=self.owner, reason="x")
        b = self.rows("product-sales", **{"from": "2026-09-01", "to": "2026-09-30"})
        self.assertEqual(b["totals"]["amount_total"], "880.00")


class ExpenseReportTests(TableBase):
    def setUp(self):
        super().setUp()
        self.spend("100.00", date(2026, 9, 3))
        self.spend("50.00", date(2026, 9, 3))
        self.spend("25.00", date(2026, 9, 9))

    def test_detail_and_groups(self):
        q = {"from": "2026-09-01", "to": "2026-09-30"}
        b = self.rows("expenses", **q)
        self.assertEqual(len(b["rows"]), 3)
        self.assertEqual(b["totals"]["amount"], "175.00")
        d = self.rows("expenses", group_by="day", **q)
        self.assertEqual([(r["date"], r["amount"]) for r in d["rows"]],
                         [("2026-09-03", "150.00"), ("2026-09-09", "25.00")])
        c = self.rows("expenses", group_by="category", **q)
        self.assertEqual(c["rows"][0]["amount"], "175.00")

    def test_cancelled_excluded(self):
        from apps.expenses.services import cancel_expense
        e = self.spend("1000.00", date(2026, 9, 4))
        cancel_expense(business_id=self.business.pk, expense_id=e.pk,
                       user=self.owner, reason="dup")
        b = self.rows("expenses", **{"from": "2026-09-01", "to": "2026-09-30"})
        self.assertEqual(b["totals"]["amount"], "175.00")


class ExportAndAccessTests(TableBase):
    def setUp(self):
        super().setUp()
        self.milk(self.s1, 10, ist(2026, 9, 3))
        self.s1.name = "=HYPERLINK(\"http://evil\")"
        self.s1.save()

    def test_csv_equals_json_and_is_safe(self):
        resp, rows = self.csv_rows("supplier-balances")
        js = self.rows("supplier-balances")
        self.assertEqual(rows[0], [c["label"] for c in js["columns"]])
        by_code = {r[0]: r for r in rows[1:]}
        self.assertEqual(by_code["S1"][1], "'=HYPERLINK(\"http://evil\")")
        self.assertEqual(by_code["S1"][4], "100.00")
        self.assertEqual(rows[-1][0], "TOTAL")
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertTrue(resp["Content-Disposition"].endswith('.csv"'))
        self.assertEqual(resp["Cache-Control"], "no-store")
        # JSON == CSV for money columns
        js_by = {r["code"]: r for r in js["rows"]}
        self.assertEqual(js_by["S1"]["outstanding"], by_code["S1"][4])

    def test_empty_report_exports_header_and_total(self):
        _, rows = self.csv_rows("expenses", **{"from": "2026-01-01", "to": "2026-01-31"})
        self.assertEqual(rows[0][0], "Date")
        self.assertEqual(rows[-1][0], "TOTAL")

    def test_every_endpoint_is_owner_manager_only_and_tenant_scoped(self):
        q = {"supplier-ledger": {"supplier_id": str(self.s1.pk)}}
        for name in ("supplier-balances", "supplier-ledger", "customer-outstanding",
                     "stock", "product-sales", "expenses"):
            params = q.get(name, {})
            for user, code in ((self.staff, 403), (self.outsider, 403),
                               (self.other_owner, 403), (self.manager, 200)):
                self.assertEqual(self.get(name, user, **params).status_code, code, (name, user))
            self.assertEqual(self.get(name, bogus="1", **params).status_code, 400, name)
            self.assertEqual(self.get(name, export="xml", **params).status_code, 400, name)
            self.assertEqual(self.get(name, **{**params, "from": "nope"}).status_code, 400, name)

    def test_other_business_data_never_leaks(self):
        from apps.customers.models import Customer
        # data only in the other business must not show up here
        Customer.objects.create(business=self.other_business, customer_code="ZZ", name="Z")
        b = self.rows("customer-outstanding")
        self.assertNotIn("ZZ", [r["code"] for r in b["rows"]])


class PdfExportTests(TableBase):
    def setUp(self):
        super().setUp()
        self.s1.name = "रामलाल क्षत्रिय Dairy"
        self.s1.save()
        self.milk(self.s1, 10, ist(2026, 9, 3))

    def pdf(self, name="supplier-balances", **params):
        r = self.get(name, export="pdf", **params)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r["Content-Disposition"].endswith('.pdf"'))
        return r.content

    def test_pdf_is_valid_with_hindi_and_page_numbers(self):
        data = self.pdf()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreaterEqual(data.count(b"/Type /Page\n") + data.count(b"/Type /Page "), 1)
        self.assertIn(b"Image", data)  # the Hindi name was drawn as an image

    def test_empty_pdf_still_renders(self):
        data = self.pdf("expenses", **{"from": "2026-01-01", "to": "2026-01-31"})
        self.assertTrue(data.startswith(b"%PDF"))

    def test_row_cap_is_a_400_not_a_500(self):
        from unittest import mock
        with mock.patch("apps.reports.pdf.MAX_ROWS", 0):
            r = self.get("supplier-balances", export="pdf")
        self.assertEqual(r.status_code, 400)


class ThrottleAndQueryCountTests(TableBase):
    def test_exports_are_rate_limited_but_json_is_not(self):
        from unittest import mock
        from django.core.cache import cache
        from apps.reports.common import ExportThrottle
        cache.clear()
        with mock.patch.object(ExportThrottle, "THROTTLE_RATES", {"reports_export": "2/min"}):
            codes = [self.get("supplier-balances", export="csv").status_code for _ in range(3)]
            self.assertEqual(codes, [200, 200, 429])
            self.assertEqual(self.get("supplier-balances").status_code, 200)
        cache.clear()

    def test_query_count_is_flat(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        names = ("supplier-balances", "customer-outstanding", "stock",
                 "product-sales", "expenses")
        q = {"from": "2026-09-01", "to": "2026-09-30"}

        def count(name):
            with CaptureQueriesContext(connection) as ctx:
                self.get(name, **q) if name in ("stock", "product-sales", "expenses") \
                    else self.get(name)
            return len(ctx)

        self.milk(self.s1, 5, ist(2026, 9, 5))   # one of everything first
        self.spend("10.00", date(2026, 9, 5))
        small = {n: count(n) for n in names}
        for i in range(6):
            s = self.supplier(f"Q{i}", f"Q{i}")
            self.milk(s, 5, ist(2026, 9, 6))
            self.spend("10.00", date(2026, 9, 6))
            self.deliver(self.cow, "1", ist(2026, 9, 7 + i), customer=self.customer2)
            self.sell(mode="CASH", user=self.owner, sold_at=ist(2026, 9, 8, 10 + i))
        big = {n: count(n) for n in names}
        self.assertEqual(small, big)
