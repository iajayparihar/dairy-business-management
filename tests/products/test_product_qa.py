"""QA sweep: categories, products, prices. A failing test is a finding."""
import threading
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.products.models import Product, ProductCategory, ProductPrice

User = get_user_model()
FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


class Base:
    def setUp(self):
        mk = lambda n: User.objects.create_user(n, f"{n}@x.com", "pw-123456!x")
        self.owner, self.manager, self.staff, self.outsider = (mk(n) for n in ("pq_o", "pq_m", "pq_s", "pq_x"))
        self.biz = create_business(name="PQ", slug="pq-dairy", owner=self.owner)
        self.other = create_business(name="PQ2", slug="pq-other", owner=self.outsider)
        for u, r in ((self.manager, "MANAGER"), (self.staff, "STAFF")):
            BusinessMembership.objects.create(business=self.biz, user=u, role=r, is_active=True)
        self.base = f"/api/v1/businesses/{self.biz.pk}/"

    def api(self, user=None):
        c = APIClient()
        if user:
            c.force_authenticate(user)
        return c

    def mk(self, sku="MILK1", name="Milk", **kw):
        return Product.objects.create(business=self.biz, sku=sku, name=name, **kw)

    def post(self, path, data, user=None):
        return self.api(user or self.owner).post(self.base + path, data, format="json")


@FAST
class CategoryQA(Base, TestCase):
    def test_crud_and_roles(self):
        self.assertEqual(self.post("categories/", {"name": "Milk"}).status_code, 201)
        self.assertEqual(self.post("categories/", {"name": "Ghee"}, self.manager).status_code, 201)
        self.assertEqual(self.post("categories/", {"name": "X"}, self.staff).status_code, 403)
        self.assertEqual(self.api(self.staff).get(self.base + "categories/").status_code, 200)
        self.assertEqual(self.api(self.outsider).get(self.base + "categories/").status_code, 403)

    def test_duplicate_names_400_including_case_and_deleted(self):
        self.post("categories/", {"name": "Milk"})
        ProductCategory.objects.create(business=self.biz, name="Old").delete()
        for n in ("Milk", "milk", " MILK ", "Old"):
            self.assertEqual(self.post("categories/", {"name": n}).status_code, 400, n)

    def test_validation(self):
        for body in ({}, {"name": ""}, {"name": "  "}, {"name": "x" * 101}, {"name": "ok", "bogus": 1}):
            self.assertEqual(self.post("categories/", body).status_code, 400, body)

    def test_list_paginated_and_filtered(self):
        for i in range(5):
            ProductCategory.objects.create(business=self.biz, name=f"C{i}", is_active=i % 2 == 0)
        r = self.api(self.owner).get(self.base + "categories/", {"page_size": 2}).json()
        self.assertEqual((r["count"], len(r["results"])), (5, 2))
        r = self.api(self.owner).get(self.base + "categories/", {"is_active": "false"}).json()
        self.assertEqual(r["count"], 2)

    def test_update_delete_rules(self):
        c = ProductCategory.objects.create(business=self.biz, name="Dairy")
        d = f"{self.base}categories/{c.pk}/"
        self.assertEqual(self.api(self.owner).patch(d, {"name": "Dairy 2"}, format="json").status_code, 200)
        self.mk("P1", category=c)
        self.assertEqual(self.api(self.owner).delete(d).status_code, 400)
        Product.all_objects.all().hard_delete()
        self.assertEqual(self.api(self.owner).delete(d).status_code, 204)


@FAST
class ProductQA(Base, TestCase):
    def test_create_defaults_and_roles(self):
        r = self.post("products/", {"sku": "MILK1", "name": "Cow Milk"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["unit"], "L")
        self.assertFalse(r.json()["track_stock"])
        self.assertEqual(self.post("products/", {"sku": "M2", "name": "x"}, self.staff).status_code, 403)
        self.assertEqual(self.api().post(self.base + "products/", {}, format="json").status_code, 401)

    def test_duplicate_sku_is_400_not_500(self):
        self.mk("DUP")
        Product.objects.create(business=self.biz, sku="GONE", name="g").delete()
        for s in ("DUP", " DUP ", "GONE"):
            self.assertEqual(self.post("products/", {"sku": s, "name": "x"}).status_code, 400, s)

    def test_same_sku_in_other_business_ok(self):
        Product.objects.create(business=self.other, sku="SAME", name="x")
        self.assertEqual(self.post("products/", {"sku": "SAME", "name": "y"}).status_code, 201)

    def test_validation(self):
        bad = ({}, {"sku": "A"}, {"name": "A"}, {"sku": "", "name": "x"}, {"sku": "A" * 51, "name": "x"},
               {"sku": "A", "name": "x" * 201}, {"sku": "A", "name": "x", "unit": "TON"},
               {"sku": "A", "name": "x", "bogus": 1}, {"sku": "A\x00", "name": "x"},
               {"sku": "A", "name": "x", "category": "11111111-1111-1111-1111-111111111111"})
        for body in bad:
            self.assertEqual(self.post("products/", body).status_code, 400, body)
        for v in (1, [], {}, True):
            self.assertLess(self.post("products/", {"sku": v, "name": v}).status_code, 500)

    def test_category_must_be_same_business_and_active(self):
        theirs = ProductCategory.objects.create(business=self.other, name="T")
        off = ProductCategory.objects.create(business=self.biz, name="Off", is_active=False)
        for c in (theirs, off):
            self.assertEqual(self.post("products/", {"sku": "C1", "name": "x", "category": str(c.pk)}).status_code, 400)

    def test_hindi_name(self):
        self.assertEqual(self.post("products/", {"sku": "H1", "name": "देसी घी"}).json()["name"], "देसी घी")

    def test_list_filters_search_pagination_isolation(self):
        for i in range(5):
            self.mk(f"S{i}", "Same")
        self.mk("OFF", "Off", is_active=False)
        self.mk("KG1", "Paneer", unit="KG", track_stock=True)
        Product.objects.create(business=self.other, sku="THEIRS", name="Theirs")
        seen = []
        for p in (1, 2, 3, 4):
            seen += [x["id"] for x in self.api(self.staff).get(self.base + "products/", {"page_size": 2, "page": p}).json()["results"]]
        self.assertEqual((len(seen), len(set(seen))), (7, 7))
        g = lambda q: sorted(x["sku"] for x in self.api(self.owner).get(self.base + "products/", q).json()["results"])
        self.assertEqual(g({"unit": "KG"}), ["KG1"])
        self.assertEqual(g({"track_stock": "true"}), ["KG1"])
        self.assertEqual(g({"is_active": "false"}), ["OFF"])
        self.assertEqual(g({"search": "paneer"}), ["KG1"])
        self.assertEqual(g({"search": "theirs"}), [])
        for q in ({"unit": "TON"}, {"is_active": "x"}, {"category": "nope"}):
            self.assertEqual(self.api(self.owner).get(self.base + "products/", q).status_code, 400, q)

    def test_frozen_fields_when_records_exist(self):
        p = self.mk("F1", track_stock=True)
        d = f"{self.base}products/{p.pk}/"
        with mock.patch("apps.products.serializers.product_has_records", return_value=True):
            for body in ({"unit": "KG"}, {"track_stock": False}, {"sku": "NEW"}):
                self.assertEqual(self.api(self.owner).patch(d, body, format="json").status_code, 400, body)
            self.assertEqual(self.api(self.owner).patch(d, {"name": "Renamed", "unit": "L"}, format="json").status_code, 200)
        self.assertEqual(self.api(self.owner).patch(d, {"unit": "KG"}, format="json").status_code, 200)

    def test_delete_rules(self):
        p = self.mk("D1")
        d = f"{self.base}products/{p.pk}/"
        with mock.patch("apps.products.views.product_has_records", return_value=True):
            self.assertEqual(self.api(self.owner).delete(d).status_code, 400)
        self.assertEqual(self.api(self.staff).delete(d).status_code, 403)
        self.assertEqual(self.api(self.owner).delete(d).status_code, 204)
        self.assertEqual(Product.all_objects.get(pk=p.pk).deleted_by_id, self.owner.pk)

    def test_guard_sees_every_related_table(self):
        from apps.products.guards import product_has_records
        names = {r.related_model.__name__ for r in Product._meta.related_objects}
        self.assertTrue({"Delivery", "SaleLine", "PurchaseLine", "StockMovement", "ProductionLine",
                         "StockAdjustment"} <= names)
        self.assertFalse(product_has_records(self.mk("G1")))


@FAST
class ProductPriceQA(Base, TestCase):
    def setUp(self):
        super().setUp()
        self.p = self.mk("PR1", "Milk")
        self.purl = f"products/{self.p.pk}/prices/"

    def price(self, amount="50.00", kind="SELLING", frm=None, user=None, **kw):
        body = {"price_type": kind, "unit_price": amount, **kw}
        if frm:
            body["effective_from"] = frm.isoformat()
        return self.post(self.purl, body, user)

    def test_now_and_future_and_roles(self):
        self.assertEqual(self.price().status_code, 201)
        self.assertEqual(self.price("55", frm=timezone.now() + timedelta(days=1)).status_code, 201)
        self.assertEqual(self.price("1", user=self.staff).status_code, 403)

    def test_change_closes_previous_and_schedule_insert(self):
        self.price("50")
        t = timezone.now() + timedelta(days=10)
        self.price("60", frm=t + timedelta(days=10))
        self.assertEqual(self.price("55", frm=t).status_code, 201)
        mid = ProductPrice.objects.get(unit_price="55.00")
        self.assertEqual(mid.effective_to, t + timedelta(days=10))
        self.assertEqual(ProductPrice.objects.get(unit_price="50.00").effective_to, mid.effective_from)

    def test_backdating_and_clock_skew(self):
        self.assertEqual(self.price(frm=timezone.now() - timedelta(days=1)).status_code, 400)
        self.assertEqual(self.price(frm=timezone.now() - timedelta(seconds=30)).status_code, 201)

    def test_validation(self):
        base = timezone.now().isoformat()
        for body in ({}, {"price_type": "SELLING"}, {"unit_price": "1"},
                     {"price_type": "NOPE", "unit_price": "1"},
                     {"price_type": "SELLING", "unit_price": "-1"},
                     {"price_type": "SELLING", "unit_price": "1.234"},
                     {"price_type": "SELLING", "unit_price": "abc"},
                     {"price_type": "SELLING", "unit_price": "NaN"},
                     {"price_type": "SELLING", "unit_price": "1e30"},
                     {"price_type": "SELLING", "unit_price": "1", "effective_from": "2030-01-01T10:00:00"},
                     {"price_type": "SELLING", "unit_price": "1", "effective_from": "junk"},
                     {"price_type": "SELLING", "unit_price": "1", "bogus": 1}):
            self.assertEqual(self.post(self.purl, body).status_code, 400, body)

    def test_same_start_twice(self):
        t = timezone.now() + timedelta(days=2)
        self.assertEqual(self.price("1", frm=t).status_code, 201)
        self.assertEqual(self.price("2", frm=t).status_code, 400)

    def test_cost_prices_hidden_from_staff(self):
        self.price("10", kind="PURCHASE")
        self.price("50", kind="SELLING")
        rows = self.api(self.staff).get(self.base + self.purl).json()["results"]
        self.assertEqual({r["price_type"] for r in rows}, {"SELLING"})
        rows = self.api(self.manager).get(self.base + self.purl).json()["results"]
        self.assertEqual({r["price_type"] for r in rows}, {"SELLING", "PURCHASE"})
        r = self.api(self.staff).get(self.base + self.purl, {"price_type": "PURCHASE"}).json()
        self.assertEqual(r["count"], 0)

    def test_inactive_foreign_deleted_product(self):
        Product.objects.filter(pk=self.p.pk).update(is_active=False)
        self.assertEqual(self.price().status_code, 400)
        theirs = Product.objects.create(business=self.other, sku="T", name="t")
        self.assertEqual(self.api(self.owner).get(f"{self.base}products/{theirs.pk}/prices/").status_code, 404)

    def test_zero_price_allowed_and_two_types_independent(self):
        self.assertEqual(self.price("0").status_code, 201)
        self.assertEqual(self.price("5", kind="PURCHASE").status_code, 201)


@FAST
class ProductRaceQA(Base, TransactionTestCase):
    def test_parallel_duplicate_sku(self):
        codes = []

        def go():
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                codes.append(c.post(self.base + "products/", {"sku": "RACE", "name": "n"}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertEqual(codes.count(201), 1, codes)

    def test_parallel_price_changes_never_overlap(self):
        p = Product.objects.create(business=self.biz, sku="PRC", name="p")
        codes = []

        def go(i):
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                t = (timezone.now() + timedelta(days=1, minutes=i)).isoformat()
                codes.append(c.post(f"{self.base}products/{p.pk}/prices/",
                                    {"price_type": "SELLING", "unit_price": str(10 + i), "effective_from": t},
                                    format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        rows = list(ProductPrice.objects.filter(product=p).order_by("effective_from"))
        for a, b in zip(rows, rows[1:]):
            self.assertEqual(a.effective_to, b.effective_from)
        self.assertIsNone(rows[-1].effective_to)
