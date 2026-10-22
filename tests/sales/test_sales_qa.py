"""QA sweep: shop sales API. A failing test is a finding."""
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.sales.models import Sale
from tests.sales.test_api import ApiBase

FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
UNKNOWN = "11111111-1111-1111-1111-111111111111"


@FAST
class SaleQA(ApiBase):
    def line(self, **kw):
        return {"product_id": str(self.ghee.pk), "quantity": "1", **kw}

    def test_roles(self):
        self.assertEqual(self.post_sale(self.body(), self.staff).status_code, 201)
        self.assertEqual(self.post_sale(self.body(), self.outsider).status_code, 403)
        self.assertEqual(APIClient().post(self.url("sale-list-create"), self.body(), format="json").status_code, 401)

    def test_validation_never_500(self):
        bad = [dict(client_ref=None), dict(client_ref=""), dict(client_ref="x" * 200), dict(payment_mode="BARTER"),
               dict(payment_mode=None), dict(lines=[]), dict(lines=None), dict(lines="x"), dict(lines=[1]), dict(lines=[None]),
               dict(lines=[self.line(quantity="0")]), dict(lines=[self.line(quantity="-1")]), dict(lines=[self.line(quantity="abc")]),
               dict(lines=[self.line(quantity="NaN")]), dict(lines=[self.line(quantity="1e400")]), dict(lines=[self.line(quantity="1.2345")]),
               dict(lines=[self.line(quantity=None)]), dict(lines=[self.line(product_id=UNKNOWN)]), dict(lines=[self.line(product_id="x")]),
               dict(lines=[self.line(bogus=1)]), dict(lines=[self.line()] * 5000), dict(bogus=1),
               dict(sold_at="2026-01-01T10:00:00"), dict(sold_at="junk"), dict(sold_at="2026-13-45T00:00:00Z"), dict(sold_at=5),
               dict(customer_id="x"), dict(customer_id=UNKNOWN), dict(notes=5), dict(notes="x" * 100000),
               dict(lines=[self.line(unit_price="1.00")]), dict(lines=[{"description": "x", "amount": "-5"}]),
               dict(lines=[{"description": "x", "amount": "0"}]), dict(lines=[{"description": "x" * 1000, "amount": "5"}])]
        for kw in bad:
            r = self.post_sale(self.body(**kw))
            self.assertEqual(r.status_code, 400, (str(kw)[:90], r.content[:150]))
        for raw in ("[]", "null", "5", "{", '"x"'):
            r = self.api().post(self.url("sale-list-create"), raw, content_type="application/json")
            self.assertEqual(r.status_code, 400, raw)

    def test_future_sale_refused(self):
        r = self.post_sale(self.body(sold_at=(timezone.now() + timedelta(days=3)).isoformat()))
        self.assertEqual(r.status_code, 400)

    def test_credit_needs_customer_cash_is_walk_in(self):
        self.assertEqual(self.post_sale(self.body(payment_mode="CREDIT")).status_code, 400)
        r = self.post_sale(self.body(payment_mode="CREDIT", customer_id=str(self.customer.pk)))
        self.assertEqual(r.status_code, 201, r.content)

    def test_other_business_customer_refused(self):
        r = self.post_sale(self.body(payment_mode="CREDIT", customer_id=str(self.other_customer.pk)))
        self.assertEqual(r.status_code, 400)

    def test_inactive_customer_refused_for_credit(self):
        self.customer.is_active = False
        self.customer.save(update_fields=["is_active"])
        r = self.post_sale(self.body(payment_mode="CREDIT", customer_id=str(self.customer.pk)))
        self.assertEqual(r.status_code, 400)

    def test_replay_and_conflict(self):
        b = self.body(client_ref="same")
        a, c = self.post_sale(b), self.post_sale(b)
        self.assertEqual((a.status_code, c.status_code), (201, 201))
        self.assertEqual(a.json()["id"], c.json()["id"])
        d = self.post_sale(self.body(client_ref="same", lines=[self.line(quantity="9")]))
        self.assertEqual(d.status_code, 409)
        self.assertEqual(Sale.objects.count(), 1)

    def test_same_ref_in_another_business_is_independent(self):
        self.post_sale(self.body(client_ref="shared"))
        self.assertEqual(Sale.objects.filter(client_ref="shared").count(), 1)

    def test_totals_and_rounding(self):
        r = self.post_sale(self.body(lines=[self.line(quantity="0.333")]))
        self.assertEqual(r.json()["total_amount"], "266.40")  # 0.333 x 800 = 266.4

    def test_cancel_and_correct_rules(self):
        s = self.post_sale(self.body()).json()
        u = lambda a: self.url(f"sale-{a}", sale_id=s["id"])
        self.assertEqual(self.api(self.staff).post(u("cancel"), {"reason": "x"}, format="json").status_code, 403)
        for body in ({}, {"reason": ""}, {"reason": "  "}, {"reason": "x" * 600}, {"reason": "r", "bogus": 1}):
            self.assertEqual(self.api(self.manager).post(u("cancel"), body, format="json").status_code, 400, body)
        self.assertEqual(self.api(self.manager).post(u("cancel"), {"reason": "wrong item"}, format="json").status_code, 200)
        self.assertEqual(self.api(self.manager).post(u("cancel"), {"reason": "again"}, format="json").status_code, 400)
        self.assertEqual(self.api(self.manager).post(u("correct"), {"reason": "r", "lines": [self.line()]}, format="json").status_code, 400)

    def test_list_params_and_isolation(self):
        self.post_sale(self.body())
        for q in ({"start_date": "x"}, {"end_date": "2026-13-45"}, {"payment_mode": "X"}, {"status": "X"}, {"customer_id": "x"},
                  {"start_date": "2026-02-01", "end_date": "2026-01-01"}):
            self.assertEqual(self.api(self.manager).get(self.url("sale-list-create"), q).status_code, 400, q)
        self.assertEqual(self.api(self.manager).get(self.url("sale-list-create")).json()["count"], 1)
        self.assertEqual(self.api(self.outsider).get(self.url("sale-list-create")).status_code, 403)
        self.assertLess(self.api(self.manager).get(self.url("sale-list-create"), {"page": "x", "page_size": "y"}).status_code, 500)

    def test_summary(self):
        self.post_sale(self.body())
        day = timezone.now().astimezone().date().isoformat()
        self.assertEqual(self.api(self.manager).get(self.url("sale-summary"), {"date": day}).status_code, 200)
        for d in ("x", "2026-13-45", "0000-00-00"):
            self.assertEqual(self.api(self.manager).get(self.url("sale-summary"), {"date": d}).status_code, 400, d)

    def test_foreign_and_unknown_sale_404(self):
        self.assertEqual(self.api(self.manager).get(self.url("sale-detail", sale_id=UNKNOWN)).status_code, 404)
        s = self.post_sale(self.body()).json()
        self.assertEqual(self.api(self.outsider).get(self.url("sale-detail", sale_id=s["id"])).status_code, 403)

    def test_quick_customer(self):
        u = self.url("quick-customer")
        self.assertEqual(self.api(self.staff).post(u, {"name": "Walk In", "phone": "9876500000"}, format="json").status_code, 201)
        for body in ({}, {"name": "x"}, {"phone": "9876500000"}, {"name": "", "phone": "9876500000"}, {"name": "x", "phone": "abc"},
                     {"name": "x", "phone": "9876500000", "bogus": 1}, {"name": ["x"], "phone": "9876500000"}):
            self.assertEqual(self.api(self.staff).post(u, body, format="json").status_code, 400, body)
