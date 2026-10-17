"""Stock balance API: shape, filters, roles, tenant isolation, timezone."""
from django.urls import reverse

from apps.inventory.services import stock_balances, stock_net_by_date
from tests.billing.base import ist
from tests.inventory.base import D, InvBase


class BalanceApiTests(InvBase):
    def url(self, name, business=None, **kw):
        return reverse(f"inventory:{name}", kwargs={
            "business_id": (business or self.business).pk, **kw})

    def test_list_shows_tracked_only_with_negative_flag(self):
        self.stock_in(self.paneer, "5")
        self.sell([{"product_id": self.paneer.pk, "quantity": "7"}])
        r = self.api().get(self.url("stock-list"))
        self.assertEqual(r.status_code, 200)
        rows = {x["sku"]: x for x in r.data["results"]}
        self.assertEqual(rows["PNR"]["balance"], "-2.000")
        self.assertTrue(rows["PNR"]["is_negative"])
        self.assertNotIn("GHEE", rows)  # ghee does not track stock
        neg = self.api().get(self.url("stock-list"), {"negative": "true"})
        self.assertEqual([x["sku"] for x in neg.data["results"]], ["PNR"])
        none = self.api().get(self.url("stock-list"), {"search": "zzz"})
        self.assertEqual(none.data["count"], 0)

    def test_zero_balance_product_without_ledger_rows_is_listed(self):
        r = self.api().get(self.url("stock-list"))
        self.assertEqual(r.data["results"][0]["balance"], "0.000")

    def test_detail_has_movements_and_date_filter(self):
        self.stock_in(self.paneer, "3", at=ist(2026, 5, 1))
        self.stock_in(self.paneer, "4", at=ist(2026, 5, 10))
        r = self.api().get(self.url("stock-detail", product_id=self.paneer.pk))
        self.assertEqual((r.data["balance"], r.data["count"]), ("7.000", 2))
        self.assertEqual(r.data["results"][0]["direction"], "IN")
        f = self.api().get(self.url("stock-detail", product_id=self.paneer.pk),
                           {"from": "2026-05-10", "to": "2026-05-10"})
        self.assertEqual((f.data["balance"], f.data["count"]), ("7.000", 1))
        bad = self.api().get(self.url("stock-detail", product_id=self.paneer.pk),
                             {"from": "2026-05-11", "to": "2026-05-10"})
        self.assertEqual(bad.status_code, 400)

    def test_untracked_product_detail_is_404(self):
        r = self.api().get(self.url("stock-detail", product_id=self.ghee.pk))
        self.assertEqual(r.status_code, 404)

    def test_roles_and_tenant(self):
        self.assertEqual(self.api(self.staff).get(self.url("stock-list")).status_code, 200)
        self.assertEqual(self.api(self.outsider).get(self.url("stock-list")).status_code, 403)
        other = self.api(self.other_owner).get(self.url("stock-list"))
        self.assertEqual(other.status_code, 403)  # not a member of this business
        anon = type(self.c)().get(self.url("stock-list"))
        self.assertIn(anon.status_code, (401, 403))

    def test_balances_match_single_balance(self):
        self.stock_in(self.paneer, "2.5")
        self.assertEqual(stock_balances(business_id=self.business.pk)[self.paneer.pk], D("2.5"))

    def test_net_by_date_uses_passed_timezone(self):
        from zoneinfo import ZoneInfo
        # 00:30 IST on 1 May is 30 Apr 19:00 UTC.
        self.stock_in(self.paneer, "1", at=ist(2026, 5, 1, 0, 30))
        kw = dict(business_id=self.business.pk, product_id=self.paneer.pk,
                  basis="business", date_from=ist(2026, 4, 30).date(),
                  date_to=ist(2026, 5, 2).date())
        days = stock_net_by_date(**kw, tz=ZoneInfo("Asia/Kolkata"))
        self.assertEqual(list(days), [ist(2026, 5, 1).date()])
        utc = stock_net_by_date(**kw, tz=ZoneInfo("UTC"))
        self.assertEqual(list(utc), [ist(2026, 4, 30).date()])
