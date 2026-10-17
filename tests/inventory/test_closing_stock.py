"""Month-end closing stock: quantities, owner-set cost price, comparison."""
from django.urls import reverse

from apps.inventory.models import StockMovement
from apps.products.models import ProductPrice
from tests.billing.base import ist
from tests.inventory.base import D, InvBase


class ClosingBase(InvBase):
    def setUp(self):
        super().setUp()
        self.dahi = self.tracked("DAHI", "Dahi")

    def cost(self, product, rate, start, end=None):
        return ProductPrice.objects.create(
            business=self.business, product=product, price_type="PURCHASE",
            unit_price=D(rate), effective_from=start, effective_to=end)

    def get(self, params, user=None):
        url = reverse("reports:closing-stock", kwargs={"business_id": self.business.pk})
        return self.api(user or self.manager).get(url, params)

    def item(self, response, product):
        return next(i for i in response.data["items"] if i["product_id"] == str(product.pk))


class ClosingStockTests(ClosingBase):
    def test_value_compare_and_rate_at_month_end(self):
        self.stock_in(self.paneer, 10, at=ist(2026, 9, 10))
        self.stock_in(self.paneer, 4, at=ist(2026, 10, 5))
        self.cost(self.paneer, "100", ist(2026, 8, 1), ist(2026, 10, 1, 0))
        self.cost(self.paneer, "120", ist(2026, 10, 1, 0))
        sept = self.get({"month": "2026-09"})
        i = self.item(sept, self.paneer)
        self.assertEqual((i["opening_qty"], i["closing_qty"], i["cost_rate"], i["closing_value"]),
                         ("0.000", "10.000", "100.00", "1000.00"))
        self.assertEqual(sept.data["total_closing_value"], "1000.00")
        self.assertEqual(i["value_change"], "1000.00")  # nothing the month before
        octo = self.get({"month": "2026-10"})
        i = self.item(octo, self.paneer)
        self.assertEqual((i["closing_qty"], i["cost_rate"], i["closing_value"]),
                         ("14.000", "120.00", "1680.00"))
        self.assertEqual((i["previous_closing_qty"], i["qty_change"], i["value_change"]),
                         ("10.000", "4.000", "680.00"))
        self.assertEqual(octo.data["compare"], {
            "previous_month": "2026-09", "previous_total_closing_value": "1000.00",
            "stock_gain_loss": "680.00"})

    def test_missing_cost_price_is_listed_not_guessed(self):
        self.stock_in(self.dahi, 6, at=ist(2026, 9, 10))
        self.stock_in(self.paneer, 1, at=ist(2026, 9, 10))
        self.cost(self.paneer, "50", ist(2026, 8, 1))
        r = self.get({"month": "2026-09"})
        i = self.item(r, self.dahi)
        self.assertEqual((i["cost_rate"], i["closing_value"], i["value_change"]),
                         (None, None, None))
        self.assertEqual([u["name"] for u in r.data["unvalued"]], ["Dahi"])
        self.assertEqual(r.data["unvalued"][0]["closing_qty"], "6.000")
        self.assertEqual(r.data["total_closing_value"], "50.00")  # dahi not counted as 0

    def test_month_boundaries_use_business_timezone(self):
        self.stock_in(self.paneer, 1, at=ist(2026, 9, 30, 23, 59))
        self.stock_in(self.paneer, 2, at=ist(2026, 10, 1, 0, 0))
        self.cost(self.paneer, "10", ist(2026, 1, 1))
        sept, octo = self.get({"month": "2026-09"}), self.get({"month": "2026-10"})
        self.assertEqual(self.item(sept, self.paneer)["closing_qty"], "1.000")
        i = self.item(octo, self.paneer)
        self.assertEqual((i["opening_qty"], i["closing_qty"]), ("1.000", "3.000"))

    def test_untouched_products_are_left_out_and_negative_is_flagged(self):
        StockMovement.objects.create(
            business=self.business, product=self.paneer, movement_type="DELIVERY",
            quantity=D("2"), occurred_at=ist(2026, 9, 5))
        self.cost(self.paneer, "10", ist(2026, 1, 1))
        r = self.get({"month": "2026-09"})
        self.assertEqual([i["sku"] for i in r.data["items"]], ["PNR"])
        i = r.data["items"][0]
        self.assertEqual((i["closing_qty"], i["is_negative"], i["closing_value"]),
                         ("-2.000", True, "-20.00"))

    def test_year_wrap_and_compare_off(self):
        self.stock_in(self.paneer, 3, at=ist(2025, 12, 20))
        self.cost(self.paneer, "10", ist(2025, 1, 1))
        jan = self.get({"month": "2026-01"})
        self.assertEqual(jan.data["compare"]["previous_month"], "2025-12")
        self.assertEqual(self.item(jan, self.paneer)["value_change"], "0.00")
        off = self.get({"month": "2026-01", "compare": "false"})
        self.assertNotIn("compare", off.data)
        self.assertNotIn("value_change", off.data["items"][0])

    def test_param_roles_and_tenant(self):
        for month in ("", "2026-13", "2026-00", "abc", "2026-1", "26-01-01"):
            self.assertEqual(self.get({"month": month}).status_code, 400, month)
        for user in (self.staff, self.outsider):
            self.assertEqual(self.get({"month": "2026-09"}, user=user).status_code, 403)
        self.stock_in(self.paneer, 3, at=ist(2026, 9, 10))
        url = reverse("reports:closing-stock", kwargs={"business_id": self.other_business.pk})
        theirs = self.api(self.other_owner).get(url, {"month": "2026-09"})
        self.assertEqual((theirs.status_code, theirs.data["items"]), (200, []))
