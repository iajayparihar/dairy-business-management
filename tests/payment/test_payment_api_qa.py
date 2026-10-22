"""QA sweep: supplier payments API. A failing test is a finding."""
import threading
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.payments.models import SupplierPayment
from apps.products.models import Product
from apps.purchases.services import create_purchase, receive_purchase
from apps.suppliers.models import Supplier

User = get_user_model()
FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
UNKNOWN = "11111111-1111-1111-1111-111111111111"


class Base:
    def setUp(self):
        mk = lambda n: User.objects.create_user(n, f"{n}@x.com", "pw-123456!x")
        self.owner, self.manager, self.staff, self.outsider = (mk(n) for n in ("pa_o", "pa_m", "pa_s", "pa_x"))
        self.biz = create_business(name="PA", slug="pa-dairy", owner=self.owner)
        self.other = create_business(name="PA2", slug="pa-other", owner=self.outsider)
        for u, r in ((self.manager, "MANAGER"), (self.staff, "STAFF")):
            BusinessMembership.objects.create(business=self.biz, user=u, role=r, is_active=True)
        self.sup = Supplier.objects.create(business=self.biz, supplier_code="PS1", name="S")
        self.prod = Product.objects.create(business=self.biz, sku="FEED", name="Feed", unit="KG")
        self.base = f"/api/v1/businesses/{self.biz.pk}/"
        self.url = f"{self.base}suppliers/{self.sup.pk}/payments/"
        self.n = 0

    def api(self, user=None):
        c = APIClient()
        if user:
            c.force_authenticate(user)
        return c

    def purchase(self, total="1000.00"):
        self.n += 1
        p = create_purchase(business_id=self.biz.pk, supplier_id=self.sup.pk, purchase_number=f"PO-{self.n}",
                            purchased_at=timezone.now() - timedelta(hours=2), user=self.owner,
                            lines=[{"product_id": str(self.prod.pk), "quantity": "1", "unit_price": total}])
        receive_purchase(purchase_id=p.pk, business_id=self.biz.pk, user=self.owner)
        return p

    def body(self, **kw):
        self.n += 1
        d = {"payment_number": f"PAY-{self.n}", "paid_at": (timezone.now() - timedelta(minutes=5)).isoformat(),
             "amount": "100.00"}
        d.update(kw)
        return {k: v for k, v in d.items() if v is not None}

    def pay(self, user=None, **kw):
        return self.api(user or self.owner).post(self.url, self.body(**kw), format="json")

    def balance(self):
        return self.api(self.owner).get(f"{self.base}suppliers/{self.sup.pk}/balance/").json()


@FAST
class SupplierPaymentQA(Base, TestCase):
    def test_roles_and_auth(self):
        self.assertEqual(self.pay(self.manager).status_code, 201)
        for u in (self.staff, self.outsider):
            self.assertEqual(self.pay(u).status_code, 403)
            self.assertEqual(self.api(u).get(self.url).status_code, 403)
        self.assertEqual(APIClient().post(self.url, self.body(), format="json").status_code, 401)

    def test_payment_pays_oldest_first_and_balance_math(self):
        a, b = self.purchase("100.00"), self.purchase("200.00")
        self.pay(amount="150.00")
        bal = self.balance()
        self.assertEqual((bal["outstanding_amount"], bal["available_credit"]), ("150.00", "0.00"))

    def test_overpayment_becomes_advance_never_blocked(self):
        self.purchase("100.00")
        self.assertEqual(self.pay(amount="250.00").status_code, 201)
        bal = self.balance()
        self.assertEqual((bal["outstanding_amount"], bal["available_credit"]), ("0.00", "150.00"))

    def test_money_is_string_never_float(self):
        self.purchase("100.00")
        self.pay(amount="40.00")
        self.assertIsInstance(self.balance()["outstanding_amount"], str)
        pid = self.api(self.owner).get(self.url).json()["results"][0]["id"]
        d = self.api(self.owner).get(f"{self.base}payments/{pid}/").json()
        self.assertIsInstance(d["amounts"]["amount"] if "amount" in d["amounts"] else d["amount"], str)
        for a in d["allocations"]:
            for k in ("amount", "unwound", "effective"):
                self.assertIsInstance(a[k], str, k)

    def test_validation(self):
        bad = ({"amount": "0"}, {"amount": "-5"}, {"amount": "abc"}, {"amount": "1.234"}, {"amount": "NaN"},
               {"amount": "Infinity"}, {"amount": "1" * 15}, {"amount": None},
               {"payment_number": ""}, {"payment_number": "x" * 51}, {"payment_number": None},
               {"paid_at": "2026-01-01T10:00:00"}, {"paid_at": "junk"}, {"paid_at": None},
               {"payment_method": "BITCOIN"}, {"reference_number": "x" * 101}, {"client_ref": "x" * 65},
               {"bogus": 1}, {"supplier": "x"})
        for kw in bad:
            r = self.pay(**kw)
            self.assertEqual(r.status_code, 400, (kw, r.content[:150]))

    def test_future_dated_payment_refused(self):
        self.assertEqual(self.pay(paid_at=(timezone.now() + timedelta(days=5)).isoformat()).status_code, 400)

    def test_duplicate_payment_number_is_400_per_business(self):
        self.assertEqual(self.pay(payment_number="DUP").status_code, 201)
        r = self.pay(payment_number="DUP")
        self.assertIn(r.status_code, (400, 409))
        self.assertEqual(SupplierPayment.objects.count(), 1)

    def test_client_ref_replay_and_conflict(self):
        a = self.pay(client_ref="c1", payment_number="P-A")
        b = self.api(self.owner).post(self.url, {**self.body(payment_number="P-A", client_ref="c1"),
                                                 "paid_at": a.json()["paid_at"]}, format="json")
        self.assertEqual(a.status_code, 201)
        self.assertIn(b.status_code, (200, 201, 409))
        self.assertEqual(SupplierPayment.objects.count(), 1)
        c = self.pay(client_ref="c1", payment_number="P-B", amount="999.00")
        self.assertEqual(c.status_code, 409)

    def test_inactive_deleted_foreign_supplier(self):
        Supplier.objects.filter(pk=self.sup.pk).update(is_active=False)
        self.assertEqual(self.pay().status_code, 404)
        theirs = Supplier.objects.create(business=self.other, supplier_code="T", name="t")
        self.assertEqual(self.api(self.owner).get(f"{self.base}suppliers/{theirs.pk}/payments/").status_code, 404)
        self.assertEqual(self.api(self.owner).get(f"{self.base}suppliers/{UNKNOWN}/balance/").status_code, 404)

    def test_list_paginated_newest_first(self):
        for _ in range(3):
            self.pay()
        r = self.api(self.owner).get(self.url, {"page_size": 2}).json()
        self.assertEqual((r["count"], len(r["results"])), (3, 2))

    def test_methods(self):
        self.assertEqual(self.api(self.owner).delete(self.url).status_code, 405)
        pid = self.pay().json()["id"]
        d = f"{self.base}payments/{pid}/"
        for m in ("put", "patch", "delete"):
            self.assertEqual(getattr(self.api(self.owner), m)(d, {}, format="json").status_code, 405, m)


@FAST
class PaymentReverseAllocateQA(Base, TestCase):
    def setUp(self):
        super().setUp()
        self.p1 = self.purchase("100.00")
        self.pid = self.pay(amount="100.00").json()["id"]
        self.d = f"{self.base}payments/{self.pid}/"

    def rev(self, body, user=None):
        return self.api(user or self.owner).post(self.d + "reverse/", body, format="json")

    def test_reverse_partial_then_rest_then_nothing_left(self):
        r = self.rev({"amount": "30.00", "reason": "bounced"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.balance()["outstanding_amount"], "30.00")
        self.assertEqual(self.rev({"reason": "rest"}).status_code, 201)
        self.assertEqual(self.balance()["outstanding_amount"], "100.00")
        self.assertEqual(self.rev({"reason": "again"}).status_code, 400)
        self.assertEqual(self.rev({"amount": "1.00", "reason": "x"}).status_code, 400)

    def test_reverse_validation_and_roles(self):
        for body in ({}, {"reason": ""}, {"reason": "  "}, {"amount": "0", "reason": "r"},
                     {"amount": "-1", "reason": "r"}, {"amount": "100.01", "reason": "r"},
                     {"amount": "abc", "reason": "r"}, {"reason": "r", "bogus": 1}):
            self.assertEqual(self.rev(body).status_code, 400, body)
        self.assertEqual(self.rev({"reason": "r"}, self.staff).status_code, 403)

    def test_reversal_client_ref_idempotent(self):
        a = self.rev({"amount": "10.00", "reason": "r", "client_ref": "rv1"})
        b = self.rev({"amount": "10.00", "reason": "r", "client_ref": "rv1"})
        c = self.rev({"amount": "20.00", "reason": "r", "client_ref": "rv1"})
        self.assertEqual((a.status_code, b.status_code, c.status_code), (201, 200, 409))
        self.assertEqual(self.balance()["outstanding_amount"], "10.00")

    def test_foreign_payment_is_404(self):
        theirs = f"/api/v1/businesses/{self.other.pk}/payments/{self.pid}/"
        self.assertEqual(self.api(self.outsider).get(theirs).status_code, 404)

    def test_allocate_rules(self):
        adv = self.pay(amount="50.00")  # purchase fully paid -> advance
        pid = adv.json()["id"]
        p2 = self.purchase("80.00")
        url = f"{self.base}payments/{pid}/allocate/"
        for body in ({}, {"purchase_id": str(p2.pk)}, {"purchase_id": "x", "amount": "1"}, {"purchase_id": UNKNOWN, "amount": "1"},
                     {"purchase_id": str(p2.pk), "amount": "0"}, {"purchase_id": str(p2.pk), "amount": "51.00"},
                     {"purchase_id": str(p2.pk), "amount": "1", "bogus": 1}):
            self.assertIn(self.api(self.owner).post(url, body, format="json").status_code, (400, 404), body)
        ok = self.api(self.owner).post(url, {"purchase_id": str(p2.pk), "amount": "50.00"}, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        again = self.api(self.owner).post(url, {"purchase_id": str(p2.pk), "amount": "50.00"}, format="json")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.api(self.owner).post(url, {"purchase_id": str(p2.pk), "amount": "10.00"}, format="json").status_code, 400)

    def test_pay_summary_and_statement_params(self):
        s = f"{self.base}suppliers/{self.sup.pk}/"
        for q in ({"days": "0"}, {"days": "367"}, {"days": "x"}, {"start_date": "2026-01-01"}, {"days": "5", "start_date": "2026-01-01", "end_date": "2026-01-02"},
                  {"start_date": "x", "end_date": "y"}):
            self.assertEqual(self.api(self.owner).get(s + "pay-summary/", q).status_code, 400, q)
        self.assertEqual(self.api(self.owner).get(s + "pay-summary/", {"days": "5"}).status_code, 200)
        for q in ({"start_date": "x"}, {"start_date": "2026-02-01", "end_date": "2026-01-01"}, {"start_date": "2026-13-45"}):
            self.assertEqual(self.api(self.owner).get(s + "statement/", q).status_code, 400, q)
        self.assertEqual(self.api(self.owner).get(s + "statement/").status_code, 200)
        self.assertEqual(self.api(self.owner).get(s + "statement/print/", {"start_date": "2020-01-01", "end_date": "2026-01-01"}).status_code, 400)
        self.assertEqual(self.api(self.staff).get(s + "statement/").status_code, 403)

    def test_statement_closing_equals_balance(self):
        self.pay(amount="30.00")
        self.rev({"amount": "10.00", "reason": "r"})
        s = f"{self.base}suppliers/{self.sup.pk}/"
        st = self.api(self.owner).get(s + "statement/", {"start_date": "2020-01-01", "end_date": "2030-01-01"}).json()
        b = self.balance()
        self.assertEqual(Decimal(str(st["closing_balance"])), Decimal(b["outstanding_amount"]) - Decimal(b["available_credit"]))


@FAST
class PaymentRaceQA(Base, TransactionTestCase):
    def test_parallel_reversals_cannot_exceed_payment(self):
        self.purchase("100.00")
        pid = self.pay(amount="100.00").json()["id"]
        codes = []

        def go():
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                codes.append(c.post(f"{self.base}payments/{pid}/reverse/", {"amount": "60.00", "reason": "r"}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertEqual(codes.count(201), 1, codes)
        self.assertEqual(self.balance()["outstanding_amount"], "60.00")

    def test_parallel_payments_same_number_one_wins(self):
        codes = []

        def go():
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                codes.append(c.post(self.url, self.body(payment_number="SAME"), format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertEqual(codes.count(201), 1, codes)
        self.assertEqual(SupplierPayment.objects.count(), 1)

    def test_parallel_payments_allocate_each_rupee_once(self):
        self.purchase("100.00")
        codes = []

        def go(i):
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                codes.append(c.post(self.url, self.body(payment_number=f"P{i}", amount="60.00"), format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        bal = self.balance()
        self.assertEqual((bal["outstanding_amount"], bal["available_credit"]), ("0.00", "140.00"))
