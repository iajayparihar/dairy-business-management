"""QA sweep: stock balance, wastage / adjustments and production API."""
from datetime import timedelta

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests.inventory.test_production import ProductionBase

FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
UNKNOWN = "11111111-1111-1111-1111-111111111111"


@FAST
class StockQA(ProductionBase):
    def u(self, name, **kw):
        from django.urls import reverse
        return reverse(f"inventory:{name}", kwargs={"business_id": self.business.pk, **kw})

    def adj(self, user=None, **kw):
        body = {"client_ref": self.ref(), "kind": "WASTAGE", "product_id": str(self.dahi.pk), "quantity": "1", "reason": "spoiled"}
        body.update(kw)
        body = {k: v for k, v in body.items() if v is not None}
        return self.api(user or self.manager).post(self.u("adjustment-list-create"), body, format="json")

    def batch(self, user=None, **kw):
        body = {"client_ref": self.ref(), "inputs": self.side((self.milk, "100")), "outputs": self.side((self.dahi, "20"))}
        body.update(kw)
        return self.api(user or self.manager).post(self.u("production-list-create"), body, format="json")

    def test_adjustment_validation_never_500(self):
        bad = [dict(kind="BARTER"), dict(kind=None), dict(quantity="0"), dict(quantity="-1"), dict(quantity="abc"),
               dict(quantity="NaN"), dict(quantity="1.2345"), dict(quantity="9" * 13), dict(quantity=None),
               dict(reason=""), dict(reason="  "), dict(reason="x" * 1000), dict(reason=None), dict(client_ref=""),
               dict(client_ref="x" * 100), dict(product_id=UNKNOWN), dict(product_id="x"), dict(bogus=1),
               dict(occurred_at="2026-01-01T10:00:00"), dict(occurred_at="junk"),
               dict(occurred_at=(timezone.now() + timedelta(days=5)).isoformat())]
        for kw in bad:
            r = self.adj(**kw)
            self.assertEqual(r.status_code, 400, str(kw)[:80])

    def test_untracked_product_cannot_be_adjusted(self):
        r = self.adj(product_id=str(self.ghee.pk))
        self.assertEqual(r.status_code, 400)

    def test_batch_validation_never_500(self):
        ok = self.side((self.milk, "100"))
        bad = [dict(inputs=[]), dict(outputs=[]), dict(inputs=None), dict(inputs="x"), dict(inputs=[1]),
               dict(inputs=self.side((self.milk, "0"))), dict(inputs=self.side((self.milk, "-1"))),
               dict(inputs=self.side((self.milk, "abc"))), dict(outputs=self.side((self.dahi, "1.2345"))),
               dict(inputs=[{"product_id": UNKNOWN, "quantity": "1"}]), dict(inputs=ok * 500), dict(bogus=1),
               dict(notes="x" * 100000), dict(produced_at="2026-01-01T10:00:00"),
               dict(produced_at=(timezone.now() + timedelta(days=5)).isoformat()),
               dict(client_ref=None), dict(client_ref="x" * 100)]
        for kw in bad:
            r = self.batch(**kw)
            self.assertEqual(r.status_code, 400, str(kw)[:80])

    def test_same_product_as_input_and_output_is_refused(self):
        r = self.batch(inputs=self.side((self.dahi, "5")), outputs=self.side((self.dahi, "5")))
        self.assertEqual(r.status_code, 400)

    def test_cancel_correct_reason_rules(self):
        b = self.batch().json()
        for name, body in (("production-cancel", {}), ("production-cancel", {"reason": ""}), ("production-cancel", {"reason": "x" * 600}),
                           ("production-cancel", {"reason": "r", "bogus": 1})):
            self.assertEqual(self.api(self.manager).post(self.u(name, batch_id=b["id"]), body, format="json").status_code, 400, str(body)[:60])
        self.assertEqual(self.api(self.staff).post(self.u("production-cancel", batch_id=b["id"]), {"reason": "r"}, format="json").status_code, 403)
        self.assertEqual(self.api(self.manager).post(self.u("production-cancel", batch_id=b["id"]), {"reason": "r"}, format="json").status_code, 200)
        self.assertEqual(self.api(self.manager).post(self.u("production-cancel", batch_id=b["id"]), {"reason": "again"}, format="json").status_code, 400)

    def test_balance_api_params_and_perf(self):
        for i in range(10):
            self.stock_in(self.dahi, 1)
        for q in ({"page": "x", "page_size": "y"}, {"negative": "maybe"}, {"search": "%"}):
            self.assertLess(self.api(self.staff).get(self.u("stock-list"), q).status_code, 500, q)
        for q in ({"from": "x"}, {"to": "2026-13-45"}, {"from": "2026-02-01", "to": "2026-01-01"}):
            self.assertEqual(self.api(self.staff).get(self.u("stock-detail", product_id=self.dahi.pk), q).status_code, 400, q)
        with CaptureQueriesContext(connection) as ctx:
            self.api(self.staff).get(self.u("stock-list"))
        self.assertLess(len(ctx), 10)

    def test_balance_quantities_are_strings(self):
        raw = self.api(self.staff).get(self.u("stock-list")).content.decode()
        self.assertNotRegex(raw, r'"balance":\s*-?\d')

    def test_staff_can_read_but_not_write(self):
        self.assertEqual(self.api(self.staff).get(self.u("stock-list")).status_code, 200)
        self.assertEqual(self.adj(user=self.staff).status_code, 403)
        self.assertEqual(self.batch(user=self.staff).status_code, 403)
        self.assertEqual(self.api(self.staff).get(self.u("production-list-create")).status_code, 403)
        self.assertEqual(self.api(self.outsider).get(self.u("stock-list")).status_code, 403)
