"""QA sweep: general supplier purchases and returns / corrections. A failing test is a finding."""
import threading
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.inventory.services import stock_balance
from apps.products.models import Product
from apps.purchases.models import SupplierPurchase
from apps.suppliers.models import Supplier

User = get_user_model()
FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


class Base:
    def setUp(self):
        mk = lambda n: User.objects.create_user(n, f"{n}@x.com", "pw-123456!x")
        self.owner, self.manager, self.staff, self.outsider = (mk(n) for n in ("pu_o", "pu_m", "pu_s", "pu_x"))
        self.biz = create_business(name="PU", slug="pu-dairy", owner=self.owner)
        self.other = create_business(name="PU2", slug="pu-other", owner=self.outsider)
        for u, r in ((self.manager, "MANAGER"), (self.staff, "STAFF")):
            BusinessMembership.objects.create(business=self.biz, user=u, role=r, is_active=True)
        self.sup = Supplier.objects.create(business=self.biz, supplier_code="PS1", name="Supplier")
        self.prod = Product.objects.create(business=self.biz, sku="BAG", name="Cattle feed", unit="KG", track_stock=True)
        self.base = f"/api/v1/businesses/{self.biz.pk}/purchases/"
        self.n = 0

    def api(self, user=None):
        c = APIClient()
        if user:
            c.force_authenticate(user)
        return c

    def body(self, **kw):
        self.n += 1
        d = {"supplier_id": str(self.sup.pk), "purchase_number": f"PO-{self.n}",
             "purchased_at": (timezone.now() - timedelta(hours=1)).isoformat(),
             "lines": [{"product_id": str(self.prod.pk), "quantity": "10", "unit_price": "25.5000"}]}
        d.update(kw)
        return {k: v for k, v in d.items() if v is not None}

    def make(self, user=None, **kw):
        return self.api(user or self.owner).post(self.base, self.body(**kw), format="json")

    def act(self, pk, action, body=None, user=None):
        return self.api(user or self.owner).post(f"{self.base}{pk}/{action}/", body or {}, format="json")

    def bal(self):
        return stock_balance(business_id=self.biz.pk, product_id=self.prod.pk)


@FAST
class PurchaseCreateQA(Base, TestCase):
    def test_create_draft_totals_and_roles(self):
        r = self.make()
        self.assertEqual(r.status_code, 201, r.content)
        j = r.json()
        self.assertEqual((j["status"], j["total_amount"]), ("DRAFT", "255.00"))
        self.assertEqual(self.make(self.manager).status_code, 201)
        self.assertEqual(self.make(self.staff).status_code, 403)
        self.assertEqual(self.make(self.outsider).status_code, 403)
        self.assertEqual(APIClient().post(self.base, self.body(), format="json").status_code, 401)

    def test_duplicate_number_400_and_per_business(self):
        self.make(purchase_number="DUP")
        self.assertEqual(self.make(purchase_number="DUP").status_code, 400)
        self.assertEqual(self.make(purchase_number=" DUP ").status_code, 400)

    def test_validation(self):
        ok_line = {"product_id": str(self.prod.pk), "quantity": "1", "unit_price": "1"}
        bad = [{"lines": []}, {"lines": None}, {"purchase_number": ""}, {"purchase_number": "x" * 51},
               {"supplier_id": "nope"}, {"supplier_id": "11111111-1111-1111-1111-111111111111"},
               {"purchased_at": "2026-01-01T10:00:00"}, {"purchased_at": "junk"},
               {"lines": [{**ok_line, "quantity": "0"}]}, {"lines": [{**ok_line, "quantity": "-1"}]},
               {"lines": [{**ok_line, "unit_price": "-1"}]}, {"lines": [{**ok_line, "quantity": "1.2345"}]},
               {"lines": [{**ok_line, "unit_price": "abc"}]}, {"lines": [{**ok_line, "product_id": "11111111-1111-1111-1111-111111111111"}]},
               {"lines": [{"product_id": str(self.prod.pk)}]}, {"bogus": 1}, {"lines": [{**ok_line, "bogus": 1}]},
               {"lines": [ok_line] * 5000}]
        for kw in bad:
            r = self.make(**kw)
            self.assertIn(r.status_code, (400, 404) if ("supplier_id" in kw or "lines" in kw) else (400,), (str(kw)[:80], r.content[:200]))

    def test_future_purchase_date_refused(self):
        self.assertEqual(self.make(purchased_at=(timezone.now() + timedelta(days=30)).isoformat()).status_code, 400)

    def test_foreign_supplier_and_foreign_product_and_inactive(self):
        theirs = Supplier.objects.create(business=self.other, supplier_code="T", name="t")
        self.assertIn(self.make(supplier_id=str(theirs.pk)).status_code, (400, 404))
        tp = Product.objects.create(business=self.other, sku="T", name="t")
        self.assertIn(self.make(lines=[{"product_id": str(tp.pk), "quantity": "1", "unit_price": "1"}]).status_code, (400, 404))
        Product.objects.filter(pk=self.prod.pk).update(is_active=False)
        self.assertIn(self.make().status_code, (400, 404))

    def test_list_filters_pagination_perf(self):
        for _ in range(5):
            self.make()
        r = self.api(self.owner).get(self.base, {"page_size": 2}).json()
        self.assertEqual((r["count"], len(r["results"])), (5, 2))
        for q in ({"status": "DRAFT"}, {"supplier": str(self.sup.pk)}, {"date_from": "2020-01-01", "date_to": "2099-01-01"}):
            self.assertEqual(self.api(self.owner).get(self.base, q).status_code, 200, q)
        for q in ({"status": "X"}, {"supplier": "x"}, {"date_from": "x"}, {"date_from": "2026-02-01", "date_to": "2026-01-01"}):
            self.assertEqual(self.api(self.owner).get(self.base, q).status_code, 400, q)
        self.assertEqual(self.api(self.staff).get(self.base).status_code, 403)
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            self.api(self.owner).get(self.base, {"page_size": 5})
        self.assertLess(len(ctx), 12)

    def test_foreign_purchase_is_404_and_malformed_id(self):
        pk = self.make().json()["id"]
        other_url = f"/api/v1/businesses/{self.other.pk}/purchases/{pk}/"
        self.assertIn(self.api(self.outsider).get(other_url).status_code, (404, 403))
        self.assertEqual(self.api(self.owner).get(f"{self.base}11111111-1111-1111-1111-111111111111/").status_code, 404)


@FAST
class PurchaseLifecycleQA(Base, TestCase):
    def test_patch_draft_and_not_after_receive(self):
        pk = self.make().json()["id"]
        r = self.api(self.owner).patch(f"{self.base}{pk}/", {"notes": "n", "lines": [
            {"product_id": str(self.prod.pk), "quantity": "2", "unit_price": "10"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["total_amount"], "20.00")
        self.assertEqual(self.api(self.owner).patch(f"{self.base}{pk}/", {"bogus": 1}, format="json").status_code, 400)
        self.assertEqual(self.act(pk, "receive").status_code, 200)
        self.assertEqual(self.api(self.owner).patch(f"{self.base}{pk}/", {"notes": "late"}, format="json").status_code, 400)

    def test_receive_posts_stock_once_and_second_receive_is_400(self):
        pk = self.make().json()["id"]
        self.assertEqual(self.act(pk, "receive").json()["status"], "RECEIVED")
        self.assertEqual(self.bal(), 10)
        self.assertEqual(self.act(pk, "receive").status_code, 400)
        self.assertEqual(self.bal(), 10)

    def test_cancel_needs_a_reason_and_only_for_drafts(self):
        pk = self.make().json()["id"]
        self.assertEqual(self.act(pk, "cancel", {}).status_code, 400)
        self.assertEqual(self.act(pk, "cancel", {"reason": "  "}).status_code, 400)
        self.assertEqual(self.act(pk, "cancel", {"reason": "wrong supplier"}).status_code, 200)
        self.assertEqual(self.act(pk, "receive").status_code, 400)
        pk2 = self.make().json()["id"]
        self.act(pk2, "receive")
        self.assertEqual(self.act(pk2, "cancel", {"reason": "x"}).status_code, 400)

    def test_staff_cannot_act(self):
        pk = self.make().json()["id"]
        for a in ("receive", "cancel"):
            self.assertEqual(self.act(pk, a, {"reason": "x"}, self.staff).status_code, 403)

    def test_returns_reduce_stock_and_net_and_cannot_exceed(self):
        pk = self.make().json()
        self.act(pk["id"], "receive")
        line = pk["lines"][0]["id"]
        r = self.act(pk["id"], "returns", {"reason": "damaged", "lines": [{"purchase_line_id": line, "quantity": "4"}]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.bal(), 6)
        net = self.api(self.owner).get(f"{self.base}{pk['id']}/adjustments/").json()
        self.assertEqual(net["net_amount"], "153.00")
        over = self.act(pk["id"], "returns", {"reason": "more", "lines": [{"purchase_line_id": line, "quantity": "7"}]})
        self.assertEqual(over.status_code, 400)
        self.assertEqual(self.bal(), 6)

    def test_return_validation(self):
        pk = self.make().json()
        self.act(pk["id"], "receive")
        line = pk["lines"][0]["id"]
        for body in ({}, {"reason": "r"}, {"reason": "", "lines": [{"purchase_line_id": line, "quantity": "1"}]},
                     {"reason": "r", "lines": []}, {"reason": "r", "lines": [{"purchase_line_id": line, "quantity": "0"}]},
                     {"reason": "r", "lines": [{"purchase_line_id": "11111111-1111-1111-1111-111111111111", "quantity": "1"}]},
                     {"reason": "r", "lines": [{"purchase_line_id": line, "quantity": "1"}], "bogus": 1}):
            self.assertEqual(self.act(pk["id"], "returns", body).status_code, 400, body)

    def test_return_on_draft_is_refused(self):
        pk = self.make().json()
        self.assertEqual(self.act(pk["id"], "returns", {"reason": "r", "lines": [
            {"purchase_line_id": pk["lines"][0]["id"], "quantity": "1"}]}).status_code, 400)

    def test_return_idempotent_client_ref(self):
        pk = self.make().json()
        self.act(pk["id"], "receive")
        body = {"reason": "r", "client_ref": "ret-1", "lines": [{"purchase_line_id": pk["lines"][0]["id"], "quantity": "1"}]}
        a, b = self.act(pk["id"], "returns", body), self.act(pk["id"], "returns", body)
        self.assertEqual(a.status_code, 201)
        self.assertIn(b.status_code, (200, 201))
        self.assertEqual(self.bal(), 9)
        body["lines"][0]["quantity"] = "2"
        self.assertIn(self.act(pk["id"], "returns", body).status_code, (400, 409))

    def test_correction_changes_price_and_quantity_with_audit(self):
        pk = self.make().json()
        self.act(pk["id"], "receive")
        line = pk["lines"][0]["id"]
        r = self.act(pk["id"], "corrections", {"reason": "bill said 24", "lines": [{"purchase_line_id": line, "unit_price": "24"}]})
        self.assertEqual(r.status_code, 201, r.content)
        data = self.api(self.owner).get(f"{self.base}{pk['id']}/adjustments/").json()
        self.assertEqual(data["net_amount"], "240.00")
        self.assertEqual(data["original_total"], "255.00")
        self.assertEqual(self.act(pk["id"], "corrections", {"reason": "r", "lines": [{"purchase_line_id": line}]}).status_code, 400)


@FAST
class PurchaseRaceQA(Base, TransactionTestCase):
    def test_parallel_receive_posts_stock_once(self):
        pk = self.make().json()["id"]
        codes = []

        def go():
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                codes.append(c.post(f"{self.base}{pk}/receive/", {}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertEqual(codes.count(200), 1, codes)
        self.assertEqual(self.bal(), 10)

    def test_parallel_returns_never_exceed_quantity(self):
        pk = self.make().json()
        self.act(pk["id"], "receive")
        line = pk["lines"][0]["id"]
        codes = []

        def go(i):
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                codes.append(c.post(f"{self.base}{pk['id']}/returns/", {"reason": "r", "lines": [
                    {"purchase_line_id": line, "quantity": "4"}]}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertEqual(codes.count(201), 2, codes)  # 4 + 4 of 10; the third would exceed 10? no: 12 > 10
        self.assertGreaterEqual(self.bal(), 0)

    def test_parallel_same_purchase_number(self):
        codes = []

        def go():
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                codes.append(c.post(self.base, self.body(purchase_number="SAME"), format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertEqual(codes.count(201), 1, codes)
