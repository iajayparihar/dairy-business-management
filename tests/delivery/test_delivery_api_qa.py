"""QA sweep: deliveries API. A failing test is a finding."""
import threading
from datetime import timedelta

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.inventory.services import stock_balance
from tests.routes.test_routes import Base

FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
UNKNOWN = "11111111-1111-1111-1111-111111111111"


class DBase(Base):
    def setUp(self):
        self.make()
        self.url = f"/api/v1/businesses/{self.business.pk}/deliveries/"

    def post(self, user=None, **kw):
        body = {"customer_id": str(self.cust[0].pk), "product_id": str(self.cow.pk), "quantity": "2"}
        body.update(kw)
        body = {k: v for k, v in body.items() if v is not None}
        return self.as_(user or self.boy).post(self.url, body, format="json")


@FAST
class DeliveryCreateQA(DBase, TestCase):
    def test_create_snapshots_price_and_total(self):
        r = self.post()
        self.assertEqual(r.status_code, 201, r.content)
        j = r.json()
        self.assertEqual((j["unit_price"], j["line_total"], j["status"]), ("60.00", "120.00", "DELIVERED"))

    def test_roles_any_member_but_not_outsiders(self):
        self.assertEqual(self.post(self.boy).status_code, 201)
        # one delivery per customer, product, shift and day: a second one is refused
        self.assertEqual(self.post(self.manager, shift="EVENING").status_code, 201)
        self.assertEqual(self.post(self.manager).status_code, 400)
        self.assertEqual(APIClient().post(self.url, {}, format="json").status_code, 401)

    def test_validation_never_500(self):
        bad = [dict(quantity="0"), dict(quantity="-1"), dict(quantity="abc"), dict(quantity="1.2345"), dict(quantity="NaN"),
               dict(quantity="9" * 13), dict(quantity=None), dict(customer_id=UNKNOWN), dict(customer_id="x"), dict(product_id=UNKNOWN),
               dict(shift="NIGHT"), dict(delivered_at="2026-01-01T10:00:00"), dict(delivered_at="junk"), dict(delivered_at="2026-13-45T00:00:00Z"),
               dict(unit_price="-1"), dict(client_ref="x" * 65), dict(bogus=1), dict(notes=["x"])]
        for kw in bad:
            r = self.post(**kw)
            self.assertIn(r.status_code, (400, 404), (kw, r.content[:150]))

    def test_future_delivery_refused(self):
        self.assertEqual(self.post(delivered_at=(timezone.now() + timedelta(days=2)).isoformat()).status_code, 400)

    def test_price_cannot_be_chosen_for_priced_products(self):
        self.assertEqual(self.post(unit_price="1.00").status_code, 400)
        r = self.post(product_id=str(self.other.pk), unit_price="25.00")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.post(product_id=str(self.other.pk)).status_code, 400)

    def test_inactive_customer_or_product_refused(self):
        Customer.objects.filter(pk=self.cust[0].pk).update(is_active=False)
        self.assertIn(self.post().status_code, (400, 404))

    def test_shortfall_is_flagged_not_blocked(self):
        r = self.post(quantity="50")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["stock_shortfall"], "50.000")
        self.assertEqual(self.as_(self.manager).get(self.url, {"has_shortfall": "true"}).json()["count"], 1)

    def test_client_ref_replay_and_conflict(self):
        a = self.post(client_ref="d1")
        b = self.post(client_ref="d1")
        self.assertEqual(a.status_code, 201)
        self.assertIn(b.status_code, (200, 201))
        self.assertEqual(a.json()["id"], b.json()["id"])
        self.assertEqual(Delivery.objects.count(), 1)
        c = self.post(client_ref="d1", quantity="9")
        self.assertIn(c.status_code, (400, 409))
        self.assertEqual(Delivery.objects.count(), 1)

    def test_list_filters_and_bad_params(self):
        self.post()
        self.post(shift="EVENING")
        for q in ({"date": "x"}, {"date_from": "2026-13-45"}, {"customer": "x"}, {"product": "x"}, {"shift": "N"}, {"status": "X"}, {"has_shortfall": "maybe"}):
            self.assertEqual(self.as_(self.manager).get(self.url, q).status_code, 400, q)
        self.assertEqual(self.as_(self.manager).get(self.url, {"shift": "EVENING"}).json()["count"], 1)
        self.assertLess(self.as_(self.manager).get(self.url, {"page": "x", "page_size": "y"}).status_code, 500)

    def test_money_in_detail_is_string(self):
        pk = self.post().json()["id"]
        raw = self.as_(self.manager).get(f"{self.url}{pk}/").content.decode()
        self.assertNotRegex(raw, r'"quantity":\s*\d+\.\d+')

    def test_foreign_delivery_404(self):
        self.assertEqual(self.as_(self.manager).get(f"{self.url}{UNKNOWN}/").status_code, 404)


@FAST
class DeliveryCancelCorrectQA(DBase, TestCase):
    def setUp(self):
        super().setUp()
        self.d = self.post(quantity="2").json()
        self.durl = f"{self.url}{self.d['id']}/"

    def test_cancel_rules_and_stock_return(self):
        before = stock_balance(business_id=self.business.pk, product_id=self.cow.pk)
        self.assertEqual(self.as_(self.boy).post(self.durl + "cancel/", {"reason": "x"}, format="json").status_code, 403)
        for body in ({}, {"reason": ""}, {"reason": "  "}, {"reason": "x" * 501}, {"reason": "r", "bogus": 1}):
            self.assertEqual(self.as_(self.manager).post(self.durl + "cancel/", body, format="json").status_code, 400, body)
        self.assertEqual(self.as_(self.manager).post(self.durl + "cancel/", {"reason": "customer away"}, format="json").status_code, 200)
        self.assertEqual(self.as_(self.manager).post(self.durl + "cancel/", {"reason": "again"}, format="json").status_code, 400)
        self.assertEqual(stock_balance(business_id=self.business.pk, product_id=self.cow.pk), before + 2)

    def test_correct_rules(self):
        self.assertEqual(self.as_(self.boy).patch(self.durl, {"reason": "x", "quantity": "1"}, format="json").status_code, 403)
        c = self.as_(self.manager)
        for body in ({}, {"reason": "r"}, {"reason": "r", "quantity": "0"}, {"reason": "r", "unit_price": "5"},
                     {"reason": "r", "shift": "NIGHT"}, {"reason": "r", "bogus": 1}):
            self.assertEqual(c.patch(self.durl, body, format="json").status_code, 400, body)
        r = c.patch(self.durl, {"reason": "miscount", "quantity": "3"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["quantity"], "3.000")
        self.assertEqual(r.json()["delivered_at"], self.d["delivered_at"])
        self.assertEqual(c.patch(self.durl, {"reason": "again", "quantity": "4"}, format="json").status_code, 400)  # old row is cancelled

    def test_no_delete_or_put(self):
        self.assertEqual(self.as_(self.manager).delete(self.durl).status_code, 405)
        self.assertEqual(self.as_(self.manager).put(self.durl, {}, format="json").status_code, 405)


@FAST
class DeliveryRaceQA(DBase, TransactionTestCase):
    def test_parallel_same_client_ref_delivers_once(self):
        codes = []

        def go():
            try:
                c = APIClient()
                c.force_authenticate(self.boy)
                codes.append(c.post(self.url, {"customer_id": str(self.cust[0].pk), "product_id": str(self.cow.pk),
                                               "quantity": "2", "client_ref": "same"}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertEqual(Delivery.objects.count(), 1)

    def test_parallel_cancel_and_correct(self):
        d = self.post().json()
        out = []

        def go(kind):
            try:
                c = APIClient()
                c.force_authenticate(self.manager)
                if kind == "cancel":
                    out.append(c.post(f"{self.url}{d['id']}/cancel/", {"reason": "r"}, format="json").status_code)
                else:
                    out.append(c.patch(f"{self.url}{d['id']}/", {"reason": "r", "quantity": "3"}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go, args=(k,)) for k in ("cancel", "correct", "cancel", "correct")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, out, out)
        self.assertLessEqual(Delivery.objects.filter(status="DELIVERED").count(), 1)
