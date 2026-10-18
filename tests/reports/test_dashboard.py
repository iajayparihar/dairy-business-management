"""Daily summary: every figure hand-computed from the fixtures below."""
from apps.billing.payment_services import reverse_customer_payment
from apps.purchases.services import cancel_milk_collection
from django.urls import reverse
from tests.reports.base import D, ReportBase, ist


class DailySummaryTests(ReportBase):
    DAY = (2026, 9, 10)

    def url(self, **q):
        return reverse("reports:daily-summary", kwargs={"business_id": self.business.pk})

    def get(self, user=None, **params):
        params.setdefault("date", "2026-09-10")
        return self.api(user or self.owner).get(self.url(), params)

    def setUp(self):
        super().setUp()
        d = self.DAY
        # Milk bought: 100 L (1000), 50 L cancelled, 40 L buffalo is untouched
        # (no buffalo rate), all on 10 Sep. Another can on 11 Sep is outside.
        self.milk(self.s1, 100, ist(*d, 6))
        gone = self.milk(self.s1, 50, ist(*d, 7))
        cancel_milk_collection(
            business_id=self.business.pk, collection_id=gone.pk,
            user=self.owner, reason="spilled")
        self.milk(self.s2, 30, ist(2026, 9, 11, 6))
        # Sales: 5 L cow delivery (300), 2 kg ghee delivery (1600),
        # shop: cash 400 (0.5 kg ghee), online 800 (1 kg), credit 400.
        self.deliver(self.cow, "5", ist(*d, 7))
        self.deliver(self.ghee, "2", ist(*d, 8))
        self.sell(mode="CASH", user=self.owner, sold_at=ist(*d, 10))
        self.sell(mode="ONLINE", user=self.owner, sold_at=ist(*d, 11),
                  lines=[{"product_id": self.ghee.pk, "quantity": "1"}])
        self.sell(mode="CREDIT", user=self.owner, sold_at=ist(*d, 12))
        # Shop sale of 3 L cow milk (180, cash) counts as litres sold.
        self.sell(mode="CASH", user=self.owner, sold_at=ist(*d, 13),
                  lines=[{"product_id": self.cow.pk, "quantity": "3"}])
        # Collections: 500 cash, 300 UPI, 200 cheque, 100 other. 50 of the cash
        # is reversed today (a reversal is dated when it is made).
        self.cash = self.pay("500.00", paid_at=ist(*d, 14))
        self.pay("300.00", paid_at=ist(*d, 14), payment_method="UPI")
        self.pay("200.00", paid_at=ist(*d, 14), payment_method="CHEQUE")
        self.pay("100.00", paid_at=ist(*d, 14), payment_method="OTHER")
        reverse_customer_payment(
            business_id=self.business.pk, payment_id=self.cash.pk,
            reason="bounced", user=self.owner, amount="50.00")
        # Supplier payment 400 UPI on the day.
        self.spay(self.s1, 400, ist(*d, 15), method="UPI")

    def test_known_figures(self):
        r = self.get()
        self.assertEqual(r.status_code, 200, r.content)
        b = r.json()
        self.assertEqual(b["milk"]["litres_bought"], "100.000")
        self.assertEqual(b["milk"]["litres_sold"], "8.000")  # 5 delivered + 3 shop
        self.assertEqual(b["purchases"]["milk_cost"], "1000.00")
        self.assertEqual(b["purchases"]["total"], "1000.00")  # mirror not doubled
        self.assertEqual(b["sales"]["deliveries"], "1900.00")  # 300 + 1600
        self.assertEqual(b["sales"]["shop_cash"], "580.00")    # 400 + 180
        self.assertEqual(b["sales"]["shop_online"], "800.00")
        self.assertEqual(b["sales"]["shop_credit"], "400.00")
        self.assertEqual(b["sales"]["total"], "3680.00")
        c = b["collections"]
        self.assertEqual(c["from_customer_payments"],
                         {"cash": "500.00", "online": "500.00", "other": "100.00"})
        self.assertEqual(c["cash"], "1080.00")    # 500 + 580 shop cash
        self.assertEqual(c["online"], "1300.00")  # 500 + 800 shop online
        self.assertEqual(c["other"], "100.00")
        self.assertEqual(c["total"], "2480.00")
        self.assertEqual(b["supplier_payments"]["online"], "400.00")
        self.assertEqual(b["supplier_payments"]["total"], "400.00")

    def test_empty_day_is_zeros(self):
        b = self.get(date="2026-01-05").json()
        self.assertEqual(b["milk"]["litres_bought"], "0.000")
        self.assertEqual(b["sales"]["total"], "0.00")
        self.assertEqual(b["collections"]["total"], "0.00")
        self.assertEqual(b["purchases"]["total"], "0.00")

    def test_day_boundary_is_business_local_half_open(self):
        # 23:59 IST on the 10th belongs to the 10th; 00:00 on the 11th does not.
        self.deliver(self.cow, "1", ist(2026, 9, 10, 23, 59), shift="EVENING")
        self.deliver(self.cow, "1", ist(2026, 9, 11, 0, 0))
        tenth = self.get().json()
        self.assertEqual(tenth["sales"]["deliveries"], "1960.00")
        eleventh = self.get(date="2026-09-11").json()
        self.assertEqual(eleventh["sales"]["deliveries"], "60.00")

    def test_cancelled_delivery_and_sale_are_excluded(self):
        from apps.deliveries.services import cancel_delivery
        from apps.sales.services import cancel_sale
        before = self.get().json()["sales"]["total"]
        d = self.deliver(self.cow, "1", ist(2026, 9, 10, 9), shift="EVENING")
        cancel_delivery(business_id=self.business.pk, delivery_id=d.pk,
                        user=self.owner, reason="oops")
        self.assertEqual(self.get().json()["sales"]["total"], before)

    def test_access_and_validation(self):
        self.assertEqual(self.get(self.staff).status_code, 403)
        self.assertEqual(self.get(self.outsider).status_code, 403)
        self.assertEqual(self.get(self.manager).status_code, 200)
        self.assertEqual(self.get(date="2026-13-45").status_code, 400)
        self.assertEqual(self.get(bogus="1").status_code, 400)
        self.assertEqual(self.get(self.other_owner).status_code, 403)

    def test_expenses_are_shown_by_expense_date(self):
        from datetime import date
        self.spend("150.00", date(2026, 9, 10))
        self.spend("70.00", date(2026, 9, 11))
        self.assertEqual(self.get().json()["expenses"], "150.00")

    def test_reversal_counts_on_the_day_it_was_made(self):
        from django.utils import timezone
        today = timezone.localdate().isoformat()
        b = self.get(date=today).json()
        self.assertEqual(
            b["collections"]["from_customer_payments"]["cash"], "-50.00")

    def test_exports_match_json(self):
        import csv, io
        r = self.get(export="csv")
        self.assertEqual(r.status_code, 200)
        text = b"".join(r.streaming_content).decode().lstrip("﻿")
        rows = {row[0]: row for row in csv.reader(io.StringIO(text))}
        js = self.get().json()
        self.assertEqual(rows["Purchase - total"][1], js["purchases"]["total"])
        self.assertEqual(rows["Sales - total"][1], js["sales"]["total"])
        self.assertEqual(rows["Collected - total"][1], js["collections"]["total"])
        self.assertEqual(rows["Litres sold (cow/buffalo milk)"][2], js["milk"]["litres_sold"])
        pdf = self.get(export="pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_query_count_does_not_grow_with_data(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.get()
                self.api(self.owner).get(
                    reverse("reports:profit", kwargs={"business_id": self.business.pk}),
                    {"period": "month", "from": "2026-09-01", "to": "2026-09-30"})
            return len(ctx)

        small = count()
        for i in range(5):
            s = self.supplier(f"Z{i}", f"Z{i}")
            self.milk(s, 5, ist(2026, 9, 10, 9 + i))
            self.spay(s, 10, ist(2026, 9, 10, 9 + i))
            self.deliver(self.cow, "1", ist(2026, 9, 12 + i, 16), customer=self.customer2)
        self.assertEqual(small, count())
