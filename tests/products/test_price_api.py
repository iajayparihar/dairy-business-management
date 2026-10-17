"""Product price API: history, scheduling, roles, tenant isolation."""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.products.models import ProductPrice
from tests.inventory.base import InvBase


class PriceApiTests(InvBase):
    def url(self, product=None, business=None):
        return reverse("products:product-price-list-create", kwargs={
            "business_id": (business or self.business).pk,
            "pk": (product or self.paneer).pk})

    def post(self, body, user=None, **kw):
        return self.api(user or self.manager).post(self.url(**kw), body, format="json")

    def test_schedule_and_history(self):
        soon = timezone.now() + timedelta(hours=2)
        a = self.post({"price_type": "SELLING", "unit_price": "410.50", "effective_from": soon.isoformat()})
        self.assertEqual(a.status_code, 201, a.data)
        self.assertEqual((a.data["price_type"], a.data["unit_price"], a.data["effective_to"]),
                         ("SELLING", "410.50", None))
        later = soon + timedelta(days=1)
        self.post({"price_type": "SELLING", "unit_price": "420", "effective_from": later.isoformat()})
        old = ProductPrice.objects.get(product=self.paneer, unit_price="410.50")
        self.assertIsNotNone(old.effective_to)  # closed exactly when the next starts
        hist = self.api(self.manager).get(self.url(), {"price_type": "SELLING"})
        self.assertEqual([r["unit_price"] for r in hist.data["results"]][:2], ["420.00", "410.50"])

    def test_omitted_time_means_now_and_backdating_is_refused(self):
        ok = self.post({"price_type": "PURCHASE", "unit_price": "250"})
        self.assertEqual(ok.status_code, 201, ok.data)
        past = (timezone.now() - timedelta(days=1)).isoformat()
        r = self.post({"price_type": "PURCHASE", "unit_price": "260", "effective_from": past})
        self.assertEqual(r.status_code, 400)
        self.assertIn("effective_from", r.data)

    def test_validation(self):
        before = ProductPrice.objects.filter(product=self.paneer).count()
        for body in ({}, {"price_type": "NOPE", "unit_price": "1"}, {"price_type": "SELLING"},
                     {"price_type": "SELLING", "unit_price": "-1"},
                     {"price_type": "SELLING", "unit_price": "1.234"},
                     {"price_type": "SELLING", "unit_price": "abc"},
                     {"price_type": "SELLING", "unit_price": "1e999"},
                     {"price_type": "SELLING", "unit_price": "1", "effective_from": "soon"}):
            self.assertEqual(self.post(body).status_code, 400, body)
        self.assertEqual(ProductPrice.objects.filter(product=self.paneer).count(), before)

    def test_cost_prices_are_for_owner_and_manager_only(self):
        self.post({"price_type": "PURCHASE", "unit_price": "250"})
        self.post({"price_type": "SELLING", "unit_price": "400"})
        staff = self.api(self.staff).get(self.url())
        self.assertEqual({r["price_type"] for r in staff.data["results"]}, {"SELLING"})
        staff_cost = self.api(self.staff).get(self.url(), {"price_type": "PURCHASE"})
        self.assertEqual(staff_cost.data["count"], 0)
        boss = self.api(self.owner).get(self.url())
        self.assertEqual({r["price_type"] for r in boss.data["results"]}, {"SELLING", "PURCHASE"})

    def test_roles_and_tenant(self):
        body = {"price_type": "SELLING", "unit_price": "9"}
        self.assertEqual(self.post(body, user=self.staff).status_code, 403)
        self.assertEqual(self.post(body, user=self.outsider).status_code, 403)
        self.assertEqual(self.api(self.outsider).get(self.url()).status_code, 403)
        foreign = self.product("FRN", "Foreign", "5.00", business=self.other_business)
        self.assertEqual(self.post(body, product=foreign).status_code, 404)  # not in this business
        anon = type(self.c)().post(self.url(), body, format="json")
        self.assertIn(anon.status_code, (401, 403))

    def test_inactive_product_is_a_400_not_a_500(self):
        self.paneer.is_active = False
        self.paneer.save(update_fields=["is_active"])
        r = self.post({"price_type": "SELLING", "unit_price": "9"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("product", r.data)

    def test_price_set_over_the_api_values_closing_stock(self):
        self.stock_in(self.paneer, 3, at=timezone.now() - timedelta(minutes=5))
        self.post({"price_type": "PURCHASE", "unit_price": "100"})
        month = timezone.localtime().strftime("%Y-%m")
        url = reverse("reports:closing-stock", kwargs={"business_id": self.business.pk})
        r = self.api(self.manager).get(url, {"month": month, "compare": "false"})
        self.assertEqual(r.data["total_closing_value"], "300.00")
        self.assertEqual(r.data["unvalued"], [])
