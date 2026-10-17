"""Wastage / manual adjustments: service rules, ledger posting, API contract."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.inventory.adjustments import cancel_adjustment, create_adjustment
from apps.inventory.models import StockAdjustment, StockMovement
from apps.inventory.services import stock_balance
from apps.payments.services import ClientRefConflict
from tests.inventory.base import D, InvBase


class AdjustmentBase(InvBase):
    def adjust(self, kind="WASTAGE", qty="2", product=None, user=None, **kw):
        kw.setdefault("client_ref", self.ref())
        kw.setdefault("reason", "Milk spoiled")
        return create_adjustment(
            business_id=self.business.pk, user=user or self.manager, kind=kind,
            product_id=(product or self.paneer).pk, quantity=qty, **kw)

    def balance(self, product=None):
        return stock_balance(business_id=self.business.pk,
                             product_id=(product or self.paneer).pk)


class ServiceTests(AdjustmentBase):
    def test_kinds_post_the_right_movement(self):
        self.stock_in(self.paneer, "10")
        for kind, mtype, delta in (("WASTAGE", "WASTAGE", -1), ("LOSS", "ADJUSTMENT_OUT", -1),
                                   ("GAIN", "ADJUSTMENT_IN", 1)):
            before = self.balance()
            adj = self.adjust(kind, "1")
            m = StockMovement.objects.get(stock_adjustment=adj)
            self.assertEqual(m.movement_type, mtype)
            self.assertEqual(self.balance() - before, delta)
            self.assertEqual(m.occurred_at, adj.occurred_at)

    def test_shortfall_is_flagged_and_never_blocks(self):
        self.stock_in(self.paneer, "1")
        adj = self.adjust("WASTAGE", "3")
        self.assertEqual(adj.stock_shortfall, D("2"))
        self.assertEqual(self.balance(), D("-2"))
        self.assertEqual(self.adjust("GAIN", "1").stock_shortfall, 0)

    def test_numbers_are_sequential_and_unique(self):
        a, b = self.adjust(), self.adjust()
        self.assertRegex(a.adjustment_number, r"^ADJ-\d{6}-1$")
        self.assertTrue(b.adjustment_number.endswith("-2"))

    def test_replay_and_conflict(self):
        a = self.adjust(client_ref="r1")
        self.assertEqual(self.adjust(client_ref="r1").pk, a.pk)
        self.assertEqual(StockMovement.objects.filter(stock_adjustment=a).count(), 1)
        for change in ({"qty": "3"}, {"kind": "LOSS"}, {"reason": "Other reason"}):
            with self.assertRaises(ClientRefConflict):
                self.adjust(client_ref="r1", **change)

    def test_replay_with_explicit_time_must_match(self):
        at = timezone.now() - timedelta(hours=3)
        a = self.adjust(client_ref="t", occurred_at=at)
        self.assertEqual(self.adjust(client_ref="t", occurred_at=at).pk, a.pk)
        with self.assertRaises(ClientRefConflict):
            self.adjust(client_ref="t", occurred_at=at - timedelta(hours=1))

    def test_validation(self):
        bad = [
            dict(kind="NOPE"), dict(qty="0"), dict(qty="-1"), dict(qty="1.2345"),
            dict(reason=""), dict(reason="ab"), dict(client_ref=""),
            dict(occurred_at=timezone.now() + timedelta(days=1)),
            dict(product=self.ghee),  # does not track stock
        ]
        for kw in bad:
            with self.assertRaises(ValidationError, msg=kw):
                self.adjust(**kw)
        self.assertEqual(StockAdjustment.objects.count(), 0)

    def test_other_business_product_rejected(self):
        other = self.product("OTH", "Other", "1.00", business=self.other_business)
        other.track_stock = True
        other.save(update_fields=["track_stock"])
        with self.assertRaises(ValidationError):
            self.adjust(product=other)

    def test_staff_cannot_adjust(self):
        with self.assertRaises(PermissionDenied):
            self.adjust(user=self.staff)

    def test_cancel_reverses_at_original_time(self):
        self.stock_in(self.paneer, "5")
        adj = self.adjust("WASTAGE", "2", occurred_at=timezone.now() - timedelta(days=2))
        cancel_adjustment(business_id=self.business.pk, adjustment_id=adj.pk,
                          user=self.manager, reason="Entered by mistake")
        adj.refresh_from_db()
        self.assertEqual((adj.status, adj.cancelled_by), ("CANCELLED", self.manager))
        self.assertEqual(self.balance(), D("5"))
        rev = StockMovement.objects.get(stock_adjustment=adj, reversal_of__isnull=False)
        self.assertEqual((rev.movement_type, rev.occurred_at), ("REVERSAL_IN", adj.occurred_at))
        with self.assertRaises(ValidationError):  # already cancelled
            cancel_adjustment(business_id=self.business.pk, adjustment_id=adj.pk,
                              user=self.manager, reason="again")

    def test_cancel_of_consumed_gain_goes_negative_and_is_flagged(self):
        gain = self.adjust("GAIN", "4")
        self.sell([{"product_id": self.paneer.pk, "quantity": "3"}])
        cancel_adjustment(business_id=self.business.pk, adjustment_id=gain.pk,
                          user=self.manager, reason="Counted wrong")
        gain.refresh_from_db()
        self.assertEqual(self.balance(), D("-3"))
        self.assertEqual(gain.cancel_stock_shortfall, D("3"))

    def test_cancel_needs_reason_and_manage_role(self):
        adj = self.adjust()
        with self.assertRaises(ValidationError):
            cancel_adjustment(business_id=self.business.pk, adjustment_id=adj.pk,
                              user=self.manager, reason=" ")
        with self.assertRaises(PermissionDenied):
            cancel_adjustment(business_id=self.business.pk, adjustment_id=adj.pk,
                              user=self.staff, reason="because")
        with self.assertRaises(ValidationError):  # other tenant cannot reach it
            cancel_adjustment(business_id=self.other_business.pk, adjustment_id=adj.pk,
                              user=self.other_owner, reason="because")

    def test_rows_are_immutable(self):
        adj = self.adjust()
        with self.assertRaises(ValueError):
            StockAdjustment.objects.filter(pk=adj.pk).update(quantity=9)
        with self.assertRaises(ValueError):
            StockAdjustment.objects.filter(pk=adj.pk).delete()
        with self.assertRaises(ValueError):
            adj.delete()


class ApiTests(AdjustmentBase):
    def url(self, name, business=None, **kw):
        return reverse(f"inventory:{name}", kwargs={
            "business_id": (business or self.business).pk, **kw})

    def body(self, **over):
        body = {"client_ref": self.ref(), "kind": "WASTAGE",
                "product_id": str(self.paneer.pk), "quantity": "2.5",
                "reason": "Spoiled in transit"}
        body.update(over)
        return body

    def post(self, body, user=None):
        return self.api(user or self.manager).post(
            self.url("adjustment-list-create"), body, format="json")

    def test_create_shape_replay_conflict(self):
        body = self.body(client_ref="a1")
        r = self.post(body)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["quantity"], r.data["stock_shortfall"], r.data["has_shortfall"]),
                         ("2.500", "2.500", True))
        self.assertRegex(r.data["adjustment_number"], r"^ADJ-\d{6}-1$")
        self.assertEqual(self.post(body).data["id"], r.data["id"])
        self.assertEqual(self.post({**body, "quantity": "9"}).status_code, 409)

    def test_contract_rejects_unknown_missing_and_server_fields(self):
        for extra in ("adjustment_number", "status", "stock_shortfall", "bogus"):
            r = self.post(self.body(**{extra: "x"}))
            self.assertEqual(r.status_code, 400, extra)
            self.assertIn(extra, r.data)
        body = self.body()
        del body["reason"]
        self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(self.post(self.body(occurred_at="yesterday")).status_code, 400)
        self.assertEqual(self.post(self.body(product_id="nope")).status_code, 400)
        self.assertEqual(self.post(self.body(product_id=str(self.ghee.pk))).status_code, 400)
        self.assertEqual(StockAdjustment.objects.count(), 0)

    def test_list_detail_filters_cancel(self):
        self.stock_in(self.paneer, "10")
        a = self.post(self.body(kind="LOSS", quantity="1")).data
        b = self.post(self.body(kind="WASTAGE", quantity="20")).data  # shortfall
        lst = self.api(self.manager).get(self.url("adjustment-list-create"))
        self.assertEqual(lst.data["count"], 2)
        only = self.api(self.manager).get(self.url("adjustment-list-create"), {"kind": "LOSS"})
        self.assertEqual([x["id"] for x in only.data["results"]], [a["id"]])
        short = self.api(self.manager).get(
            self.url("adjustment-list-create"), {"has_shortfall": "true"})
        self.assertEqual([x["id"] for x in short.data["results"]], [b["id"]])
        self.assertEqual(self.api(self.manager).get(
            self.url("adjustment-list-create"), {"kind": "BAD"}).status_code, 400)
        c = self.api(self.manager).post(
            self.url("adjustment-cancel", adjustment_id=a["id"]),
            {"reason": "Wrong product"}, format="json")
        self.assertEqual((c.status_code, c.data["status"]), (200, "CANCELLED"))
        again = self.api(self.manager).post(
            self.url("adjustment-cancel", adjustment_id=a["id"]),
            {"reason": "Wrong product"}, format="json")
        self.assertEqual(again.status_code, 400)
        d = self.api(self.manager).get(self.url("adjustment-detail", adjustment_id=a["id"]))
        self.assertEqual(d.data["cancel_reason"], "Wrong product")

    def test_roles_and_tenant_isolation(self):
        adj = self.post(self.body()).data
        for user in (self.staff, self.outsider):
            self.assertEqual(self.post(self.body(), user=user).status_code, 403)
            self.assertEqual(self.api(user).get(
                self.url("adjustment-list-create")).status_code, 403)
        self.assertEqual(self.api(self.owner).get(
            self.url("adjustment-detail", adjustment_id=adj["id"])).status_code, 200)
        other = self.api(self.other_owner).get(
            self.url("adjustment-detail", business=self.other_business, adjustment_id=adj["id"]))
        self.assertEqual(other.status_code, 404)
