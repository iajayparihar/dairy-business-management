"""Production batches: rules, ledger posting, cancel/correct, API contract."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import ProductionBatch, ProductionLine, StockMovement
from apps.inventory.production import cancel_batch, correct_batch, create_batch
from apps.inventory.services import stock_balance
from apps.payments.services import ClientRefConflict
from tests.inventory.base import D, InvBase


class ProductionBase(InvBase):
    def setUp(self):
        super().setUp()
        self.milk = self.cow  # tracked litres, mapped in BillFlow
        self.dahi = self.tracked("DAHI", "Dahi")
        self.cream = self.tracked("CRM", "Cream")
        self.gh = self.tracked("GH2", "Ghee tracked")

    def side(self, *pairs):
        return [{"product_id": str(p.pk), "quantity": q} for p, q in pairs]

    def make(self, inputs=None, outputs=None, user=None, **kw):
        kw.setdefault("client_ref", self.ref())
        return create_batch(
            business_id=self.business.pk, user=user or self.manager,
            inputs=inputs if inputs is not None else self.side((self.milk, "100")),
            outputs=outputs if outputs is not None else self.side((self.dahi, "20")),
            **kw)

    def bal(self, p):
        return stock_balance(business_id=self.business.pk, product_id=p.pk)


class ServiceTests(ProductionBase):
    def test_posts_out_for_inputs_in_for_outputs(self):
        self.stock_in(self.milk, "150")
        b = self.make(outputs=self.side((self.cream, "5"), (self.dahi, "20")))
        self.assertRegex(b.batch_number, r"^PRD-\d{6}-1$")
        self.assertEqual((self.bal(self.milk), self.bal(self.dahi), self.bal(self.cream)),
                         (D("50"), D("20"), D("5")))
        kinds = set(StockMovement.objects.filter(production_line__batch=b)
                    .values_list("movement_type", flat=True))
        self.assertEqual(kinds, {"PRODUCTION_OUT", "PRODUCTION_IN"})
        self.assertTrue(all(m.occurred_at == b.produced_at
                            for m in StockMovement.objects.filter(production_line__batch=b)))

    def test_input_shortfall_flagged_never_blocks(self):
        self.stock_in(self.milk, "30")
        b = self.make()
        line = b.lines.get(kind="INPUT")
        self.assertEqual(line.stock_shortfall, D("70"))
        self.assertEqual(b.lines.get(kind="OUTPUT").stock_shortfall, 0)
        self.assertEqual(self.bal(self.milk), D("-70"))

    def test_validation(self):
        inactive = self.tracked("OFF", "Off")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        foreign = self.tracked("FRN", "Foreign", business=self.other_business)
        good_in = self.side((self.milk, "10"))
        bad = [
            dict(inputs=[]), dict(outputs=[]), dict(inputs="x"),
            dict(inputs=self.side((self.milk, "1"), (self.milk, "2"))),   # repeat
            dict(outputs=self.side((self.milk, "1"))),                   # both sides
            dict(outputs=self.side((self.ghee, "1"))),                   # untracked
            dict(outputs=self.side((inactive, "1"))),
            dict(outputs=self.side((foreign, "1"))),
            dict(outputs=self.side((self.dahi, "0"))),
            dict(outputs=self.side((self.dahi, "-1"))),
            dict(outputs=self.side((self.dahi, "1.2345"))),
            dict(outputs=[{"product_id": str(self.dahi.pk), "quantity": "1", "x": 1}]),
            dict(outputs=[{"product_id": "nope", "quantity": "1"}]),
            dict(produced_at=timezone.now() + timedelta(days=1)),
            dict(client_ref=""),
        ]
        for kw in bad:
            with self.assertRaises(ValidationError, msg=kw):
                self.make(inputs=kw.pop("inputs", good_in), **kw)
        self.assertEqual(ProductionBatch.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_replay_conflict_and_side_order(self):
        ins = self.side((self.milk, "10"), (self.cream, "1"))
        outs = self.side((self.dahi, "4"))
        a = self.make(inputs=ins, outputs=outs, client_ref="r", notes="n")
        again = self.make(inputs=list(reversed(ins)), outputs=outs, client_ref="r", notes="n")
        self.assertEqual(again.pk, a.pk)
        self.assertEqual(ProductionBatch.objects.count(), 1)
        for change in (dict(outputs=self.side((self.dahi, "5"))), dict(notes="other"),
                       dict(inputs=self.side((self.milk, "10"))),
                       dict(produced_at=a.produced_at - timedelta(hours=1))):
            with self.assertRaises(ClientRefConflict):
                self.make(**{"inputs": ins, "outputs": outs, "notes": "n", **change},
                          client_ref="r")

    def test_cancel_reverses_at_original_time(self):
        self.stock_in(self.milk, "100")
        b = self.make(produced_at=timezone.now() - timedelta(days=3))
        cancel_batch(business_id=self.business.pk, batch_id=b.pk,
                     user=self.manager, reason="Wrong batch")
        b.refresh_from_db()
        self.assertEqual((b.status, b.cancelled_by, b.cancel_has_shortfall),
                         ("CANCELLED", self.manager, False))
        self.assertEqual((self.bal(self.milk), self.bal(self.dahi)), (D("100"), D("0")))
        for rev in StockMovement.objects.filter(production_line__batch=b,
                                                reversal_of__isnull=False):
            self.assertEqual(rev.occurred_at, rev.reversal_of.occurred_at)
        with self.assertRaises(ValidationError):
            cancel_batch(business_id=self.business.pk, batch_id=b.pk,
                         user=self.manager, reason="again")

    def test_cancel_after_output_consumed_flags_shortfall(self):
        b = self.make()
        self.sell([{"product_id": self.dahi.pk, "quantity": "15"}])
        cancel_batch(business_id=self.business.pk, batch_id=b.pk,
                     user=self.manager, reason="Mistake")
        b.refresh_from_db()
        self.assertTrue(b.cancel_has_shortfall)
        self.assertEqual(self.bal(self.dahi), D("-15"))

    def test_correct_replaces_at_original_time(self):
        self.stock_in(self.milk, "100")
        at = timezone.now() - timedelta(days=2)
        old = self.make(produced_at=at)
        new = correct_batch(
            business_id=self.business.pk, batch_id=old.pk, user=self.manager,
            reason="Weighed wrong", inputs=self.side((self.milk, "100")),
            outputs=self.side((self.dahi, "22")))
        old.refresh_from_db()
        self.assertEqual((old.status, new.status, new.produced_at, new.replaces_id),
                         ("CANCELLED", "POSTED", at, old.pk))
        self.assertEqual((self.bal(self.milk), self.bal(self.dahi)), (D("0"), D("22")))
        self.assertIsNone(new.client_ref)

    def test_correct_is_all_or_nothing(self):
        old = self.make()
        before = StockMovement.objects.count()
        with self.assertRaises(ValidationError):
            correct_batch(business_id=self.business.pk, batch_id=old.pk, user=self.manager,
                          reason="x", inputs=self.side((self.milk, "1")),
                          outputs=self.side((self.ghee, "1")))  # untracked
        old.refresh_from_db()
        self.assertEqual((old.status, StockMovement.objects.count()), ("POSTED", before))

    def test_roles_and_tenant(self):
        with self.assertRaises(PermissionDenied):
            self.make(user=self.staff)
        b = self.make()
        with self.assertRaises(PermissionDenied):
            cancel_batch(business_id=self.business.pk, batch_id=b.pk,
                         user=self.staff, reason="x")
        with self.assertRaises(ValidationError):
            cancel_batch(business_id=self.other_business.pk, batch_id=b.pk,
                         user=self.other_owner, reason="x")

    def test_rows_immutable_and_one_movement_per_line(self):
        b = self.make()
        line = b.lines.first()
        with self.assertRaises(ValueError):
            ProductionLine.objects.filter(pk=line.pk).update(quantity=1)
        with self.assertRaises(ValueError):
            line.save()
        with self.assertRaises(ValueError):
            line.delete()
        with self.assertRaises(ValueError):
            ProductionBatch.objects.filter(pk=b.pk).delete()
        original = StockMovement.objects.get(production_line=line, reversal_of__isnull=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockMovement.objects.create(
                business=self.business, product=line.product,
                movement_type=original.movement_type, quantity=1, production_line=line)


class ApiTests(ProductionBase):
    def url(self, name, business=None, **kw):
        return reverse(f"inventory:{name}", kwargs={
            "business_id": (business or self.business).pk, **kw})

    def body(self, **over):
        body = {"client_ref": self.ref(),
                "inputs": self.side((self.milk, "100")),
                "outputs": self.side((self.dahi, "20"))}
        body.update(over)
        return body

    def post(self, body, user=None):
        return self.api(user or self.manager).post(
            self.url("production-list-create"), body, format="json")

    def test_create_shape_yield_replay_conflict(self):
        body = self.body(client_ref="b1")
        r = self.post(body)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertRegex(r.data["batch_number"], r"^PRD-\d{6}-1$")
        self.assertEqual(r.data["input_litres"], "100.000")
        self.assertEqual(r.data["outputs"][0]["per_100_litres"], "20.000")
        self.assertEqual(r.data["inputs"][0]["stock_shortfall"], "100.000")
        self.assertNotIn("stock_shortfall", r.data["outputs"][0])
        self.assertTrue(r.data["has_shortfall"])
        self.assertEqual(self.post(body).data["id"], r.data["id"])
        changed = {**body, "outputs": self.side((self.dahi, "21"))}
        self.assertEqual(self.post(changed).status_code, 409)

    def test_yield_hidden_when_inputs_not_litres(self):
        r = self.post(self.body(inputs=self.side((self.cream, "10")),
                                outputs=self.side((self.gh, "4"))))
        self.assertIsNone(r.data["input_litres"])
        self.assertIsNone(r.data["outputs"][0]["per_100_litres"])

    def test_contract(self):
        for extra in ("batch_number", "status", "bogus"):
            r = self.post(self.body(**{extra: "x"}))
            self.assertEqual((r.status_code, extra in r.data), (400, True), extra)
        for missing in ("client_ref", "inputs", "outputs"):
            body = self.body()
            del body[missing]
            self.assertEqual(self.post(body).status_code, 400, missing)
        self.assertEqual(self.post(self.body(produced_at="yesterday")).status_code, 400)
        self.assertEqual(self.post(self.body(outputs=self.side((self.ghee, "1")))).status_code, 400)
        self.assertEqual(ProductionBatch.objects.count(), 0)

    def test_list_filters_cancel_correct(self):
        self.stock_in(self.milk, "500")
        a = self.post(self.body()).data
        b = self.post(self.body(inputs=self.side((self.milk, "900")))).data  # shortfall
        lst = self.api(self.manager).get(self.url("production-list-create"))
        self.assertEqual(lst.data["count"], 2)
        short = self.api(self.manager).get(
            self.url("production-list-create"), {"has_shortfall": "true"})
        self.assertEqual([x["id"] for x in short.data["results"]], [b["id"]])
        by_prod = self.api(self.manager).get(
            self.url("production-list-create"), {"product_id": str(self.dahi.pk)})
        self.assertEqual(by_prod.data["count"], 2)
        self.assertEqual(self.api(self.manager).get(
            self.url("production-list-create"), {"status": "BAD"}).status_code, 400)
        fixed = self.api(self.manager).post(
            self.url("production-correct", batch_id=a["id"]),
            {"reason": "Weighed wrong", "inputs": self.side((self.milk, "100")),
             "outputs": self.side((self.dahi, "25"))}, format="json")
        self.assertEqual(fixed.status_code, 201, fixed.data)
        self.assertEqual(fixed.data["replaces"], a["id"])
        old = self.api(self.manager).get(self.url("production-detail", batch_id=a["id"]))
        self.assertEqual((old.data["status"], old.data["replaced_by"]),
                         ("CANCELLED", fixed.data["id"]))
        c = self.api(self.manager).post(
            self.url("production-cancel", batch_id=b["id"]), {"reason": "Oops"}, format="json")
        self.assertEqual(c.data["status"], "CANCELLED")
        again = self.api(self.manager).post(
            self.url("production-cancel", batch_id=b["id"]), {"reason": "Oops"}, format="json")
        self.assertEqual(again.status_code, 400)
        noreason = self.api(self.manager).post(
            self.url("production-cancel", batch_id=fixed.data["id"]), {}, format="json")
        self.assertEqual(noreason.status_code, 400)

    def test_roles_and_tenant_isolation(self):
        batch = self.post(self.body()).data
        for user in (self.staff, self.outsider):
            self.assertEqual(self.post(self.body(), user=user).status_code, 403)
            self.assertEqual(self.api(user).get(
                self.url("production-list-create")).status_code, 403)
        other = self.api(self.other_owner).get(
            self.url("production-detail", business=self.other_business, batch_id=batch["id"]))
        self.assertEqual(other.status_code, 404)
