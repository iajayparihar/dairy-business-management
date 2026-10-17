"""Sales API: contract, statuses, roles, tenant isolation."""
from django.urls import reverse
from rest_framework.test import APIClient

from apps.inventory.models import StockMovement
from apps.sales.models import Sale
from tests.billing.base import ist
from tests.sales.base import D, SalesBase


class ApiBase(SalesBase):
    def setUp(self):
        super().setUp()
        self.c = APIClient()

    def api(self, user=None):
        self.c.force_authenticate(user or self.staff)
        return self.c

    def url(self, name, business=None, **kw):
        return reverse(f"sales:{name}", kwargs={
            "business_id": (business or self.business).pk, **kw})

    def post_sale(self, body, user=None):
        return self.api(user).post(self.url("sale-list-create"), body, format="json")

    def body(self, **over):
        body = {"client_ref": self.ref(), "payment_mode": "CASH",
                "lines": [{"product_id": str(self.ghee.pk), "quantity": "0.5"}]}
        body.update(over)
        return body


class CreateApiTests(ApiBase):
    def test_create_shape_and_replay(self):
        body = self.body(client_ref="r1")
        a = self.post_sale(body)
        self.assertEqual(a.status_code, 201, a.data)
        self.assertEqual(a.data["total_amount"], "400.00")  # string money
        self.assertRegex(a.data["sale_number"], r"^SHOP-\d{6}-1$")
        line = a.data["lines"][0]
        self.assertEqual((line["stock_posted"], line["stock_shortfall"]),
                         (False, "0.000"))  # ghee does not track stock
        self.assertIsNone(a.data["billed_as"])
        b = self.post_sale(body)
        self.assertEqual(b.data["id"], a.data["id"])
        conflict = self.post_sale({**body, "payment_mode": "ONLINE"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(Sale.objects.count(), 1)

    def test_tracked_product_exposes_shortfall(self):
        r = self.post_sale(self.body(lines=[
            {"product_id": str(self.paneer.pk), "quantity": "2"}]))
        self.assertEqual(r.data["lines"][0]["stock_shortfall"], "2.000")
        self.assertTrue(r.data["lines"][0]["stock_posted"])
        self.assertTrue(r.data["has_shortfall"])
        listed = self.api().get(self.url("sale-list-create"), {"has_shortfall": "true"})
        self.assertEqual(listed.data["count"], 1)

    def test_other_item_and_credit(self):
        r = self.post_sale(self.body(
            payment_mode="CREDIT", customer_id=str(self.customer.pk),
            lines=[{"description": "Bread", "amount": "35"}]))
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["customer"]["code"], "C001")
        self.assertIsNone(r.data["lines"][0]["product_id"])

    def test_rejected_fields_and_shapes(self):
        gid = str(self.ghee.pk)
        cases = [
            {"sale_number": "MINE"}, {"total_amount": "1"}, {"status": "X"},
            {"payment_mode": "CREDIT"},                      # no customer
            {"customer_id": "nope"},
            {"sold_at": "yesterday"},
            {"notes": 5},
            {"lines": []},
            {"lines": [{"product_id": gid, "quantity": "1", "amount": "5"}]},
            {"lines": [{"description": "x", "amount": "5", "product_id": None, "quantity": 1}]},
            {"lines": [{"description": "x"}]},
            {"lines": [{"product_id": gid, "quantity": "1", "unit_price": "1"}]},
            {"lines": "x"},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                r = self.post_sale(self.body(**extra))
                self.assertEqual(r.status_code, 400, r.data)
        for key in ("client_ref", "payment_mode", "lines"):
            body = self.body()
            del body[key]
            self.assertEqual(self.post_sale(body).status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_roles_and_tenancy(self):
        self.assertEqual(self.post_sale(self.body(), self.outsider).status_code, 403)
        foreign = self.api(self.other_owner).post(
            self.url("sale-list-create"), self.body(), format="json")
        self.assertEqual(foreign.status_code, 403)
        for user in (self.owner, self.manager, self.staff):
            self.assertEqual(self.post_sale(self.body(), user).status_code, 201)
        self.c.force_authenticate(None)
        self.assertIn(self.c.get(self.url("sale-list-create")).status_code, (401, 403))


class ReadAndManageApiTests(ApiBase):
    def test_list_filters_detail_and_other_business_hidden(self):
        a = self.sell([{"description": "a", "amount": "10"}], mode="CASH", user=self.owner)
        self.sell([{"description": "b", "amount": "20"}], mode="ONLINE")
        self.sell([{"description": "c", "amount": "30"}], mode="CREDIT",
                  sold_at=ist(2026, 9, 5), user=self.owner)
        url = self.url("sale-list-create")
        self.assertEqual(self.api().get(url).data["count"], 3)
        self.assertEqual(self.api().get(url, {"payment_mode": "online"}).data["count"], 1)
        self.assertEqual(self.api().get(url, {"customer_id": str(self.customer.pk)}).data["count"], 1)
        day = self.api().get(url, {"start_date": "2026-09-05", "end_date": "2026-09-05"})
        self.assertEqual(day.data["count"], 1)
        self.assertEqual(self.api().get(url, {"start_date": "x"}).status_code, 400)
        self.assertEqual(self.api().get(url, {"status": "NOPE"}).status_code, 400)
        detail = self.api().get(self.url("sale-detail", sale_id=a.pk))
        self.assertEqual(detail.data["sale_number"], a.sale_number)
        self.assertEqual(self.api(self.other_owner).get(
            self.url("sale-detail", sale_id=a.pk)).status_code, 403)

    def test_cancel_and_correct_roles_and_contract(self):
        sale = self.sell()
        cancel = self.url("sale-cancel", sale_id=sale.pk)
        self.assertEqual(self.api(self.staff).post(cancel, {"reason": "x"}, format="json").status_code, 403)
        self.assertEqual(self.api(self.owner).post(cancel, {}, format="json").status_code, 400)
        self.assertEqual(self.api(self.owner).post(cancel, {"reason": "x", "foo": 1}, format="json").status_code, 400)
        ok = self.api(self.manager).post(cancel, {"reason": "oops"}, format="json")
        self.assertEqual((ok.status_code, ok.data["status"]), (200, "CANCELLED"))
        again = self.api(self.manager).post(cancel, {"reason": "oops"}, format="json")
        self.assertEqual(again.status_code, 400)

        other = self.sell()
        corr = self.api(self.owner).post(
            self.url("sale-correct", sale_id=other.pk),
            {"reason": "qty", "payment_mode": "CASH",
             "lines": [{"product_id": str(self.ghee.pk), "quantity": "1"}]},
            format="json")
        self.assertEqual(corr.status_code, 201, corr.data)
        self.assertEqual(corr.data["replaces"], str(other.pk))
        self.assertEqual(corr.data["total_amount"], "800.00")
        bad = self.api(self.owner).post(
            self.url("sale-correct", sale_id=corr.data["id"]),
            {"reason": "x", "payment_mode": "CASH", "client_ref": "r", "lines": []},
            format="json")
        self.assertEqual(bad.status_code, 400)

    def test_summary(self):
        self.sell([{"description": "a", "amount": "10"}])
        today = Sale.objects.get().sold_at.astimezone().date().isoformat()
        r = self.api().get(self.url("sale-summary"), {"date": today})
        self.assertEqual((r.data["count"], r.data["total"]), (1, "10.00"))
        self.assertEqual(self.api().get(self.url("sale-summary")).status_code, 400)

    def test_billed_as_after_finalize(self):
        sale = self.sell([{"description": "a", "amount": "10"}], mode="CREDIT",
                         sold_at=ist(2026, 9, 5), user=self.owner)
        bill = self.finalize(self.draft())
        r = self.api().get(self.url("sale-detail", sale_id=sale.pk))
        self.assertEqual(r.data["billed_as"], bill.bill_number)


class CustomerLookupApiTests(ApiBase):
    def test_quick_customer_and_search(self):
        r = self.api().post(self.url("quick-customer"),
                            {"name": "Mohan Lal", "phone": "98765"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["customer_code"], "CUST-1")
        bad = self.api().post(self.url("quick-customer"),
                              {"name": "A", "phone": "1", "customer_code": "X"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.api(self.outsider).post(
            self.url("quick-customer"), {"name": "A", "phone": "1"}, format="json").status_code, 403)

        url = reverse("customers:customer-list-create",
                      kwargs={"business_id": self.business.pk})
        by_name = self.api().get(url, {"search": "mohan"}).json()
        self.assertEqual([c["name"] for c in by_name], ["Mohan Lal"])
        self.assertEqual(len(self.api().get(url, {"search": "98765"}).json()), 1)
        self.assertEqual(len(self.api().get(url, {"search": "zzz"}).json()), 0)
        self.assertGreaterEqual(len(self.api().get(url).json()), 3)
