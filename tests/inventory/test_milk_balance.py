"""Daily milk balance: ledger identity, buckets, timezone, visibility, API."""
from decimal import Decimal

from django.urls import reverse

from apps.inventory.models import BusinessMilkConfig, StockMovement
from apps.inventory.production import create_batch
from apps.inventory.services import stock_balance
from apps.purchases.services import create_milk_collection
from apps.suppliers.models import Supplier, SupplierMilkRate
from tests.billing.base import ist
from tests.inventory.base import D, InvBase

M = StockMovement.MovementType


class BalanceBase(InvBase):
    def mv(self, kind, qty, at, product=None, **kw):
        return StockMovement.objects.create(
            business=self.business, product=product or self.cow,
            movement_type=kind, quantity=D(str(qty)), occurred_at=at, **kw)

    def url(self):
        return reverse("reports:milk-balance", kwargs={"business_id": self.business.pk})

    def get(self, params, user=None):
        return self.api(user or self.manager).get(self.url(), params)


class LedgerTests(BalanceBase):
    def test_identity_and_buckets(self):
        d1 = ist(2026, 10, 1, 6)
        self.mv(M.PURCHASE_RECEIPT, 100, d1)
        self.mv(M.DELIVERY, 30, d1)
        self.mv(M.SALE, 5, d1)
        self.mv(M.WASTAGE, 2, d1)
        self.mv(M.PRODUCTION_OUT, 20, d1)
        self.mv(M.PURCHASE_RECEIPT, 50, ist(2026, 10, 2, 6))
        self.mv(M.PURCHASE_RECEIPT, 9, ist(2026, 10, 1, 6), product=self.buffalo)
        r = self.get({"from": "2026-10-01", "to": "2026-10-03"})
        self.assertEqual(r.status_code, 200, r.data)
        d = r.data["days"]
        self.assertEqual([x["date"] for x in d], ["2026-10-01", "2026-10-02", "2026-10-03"])
        c1 = d[0]["COW"]
        self.assertEqual(
            (c1["opening"], c1["bought"], c1["delivered"], c1["sold_shop"],
             c1["wastage"], c1["converted"], c1["closing"]),
            ("0.000", "100.000", "30.000", "5.000", "2.000", "20.000", "43.000"))
        self.assertEqual((d[1]["COW"]["opening"], d[1]["COW"]["closing"]), ("43.000", "93.000"))
        self.assertEqual((d[2]["COW"]["opening"], d[2]["COW"]["closing"]), ("93.000", "93.000"))
        self.assertEqual(d[0]["TOTAL"]["closing"], "52.000")  # 43 cow + 9 buffalo
        self.assertEqual(d[2]["TOTAL"]["closing"], "102.000")
        self.assertEqual(
            D(d[2]["COW"]["closing"]),
            stock_balance(business_id=self.business.pk, product_id=self.cow.pk))

    def test_reversal_nets_on_original_day_even_when_entered_later(self):
        original = self.mv(M.DELIVERY, 10, ist(2026, 10, 1, 6))
        self.mv(M.REVERSAL_IN, 10, original.occurred_at, reversal_of=original)
        r = self.get({"date": "2026-10-01"})
        row = r.data["days"][0]["COW"]
        self.assertEqual((row["delivered"], row["closing"]), ("0.000", "0.000"))

    def test_timezone_day_boundaries(self):
        self.mv(M.PURCHASE_RECEIPT, 1, ist(2026, 10, 1, 0, 30))   # 30 Sep in UTC
        self.mv(M.PURCHASE_RECEIPT, 2, ist(2026, 10, 1, 23, 59))
        self.mv(M.PURCHASE_RECEIPT, 4, ist(2026, 10, 2, 0, 0))
        d = self.get({"from": "2026-10-01", "to": "2026-10-02"}).data["days"]
        self.assertEqual((d[0]["COW"]["bought"], d[1]["COW"]["bought"]), ("3.000", "4.000"))
        self.assertEqual(
            self.get({"date": "2026-09-30"}).data["days"][0]["COW"]["bought"], "0.000")

    def test_negative_closing_is_flagged(self):
        self.mv(M.DELIVERY, 5, ist(2026, 10, 1, 6))
        row = self.get({"date": "2026-10-01"}).data["days"][0]["COW"]
        self.assertEqual((row["closing"], row["is_negative"]), ("-5.000", True))

    def test_production_adjustments_and_purchase_corrections(self):
        self.stock_in(self.cow, 100, at=ist(2026, 10, 1, 5))
        self.mv(M.ADJUSTMENT_IN, 3, ist(2026, 10, 1, 8))
        self.mv(M.ADJUSTMENT_OUT, 1, ist(2026, 10, 1, 8))
        dahi = self.tracked("DAHI", "Dahi")
        create_batch(
            business_id=self.business.pk, user=self.manager, client_ref="b",
            produced_at=ist(2026, 10, 1, 9),
            inputs=[{"product_id": str(self.cow.pk), "quantity": "40"}],
            outputs=[{"product_id": str(dahi.pk), "quantity": "8"}])
        row = self.get({"date": "2026-10-01"}).data["days"][0]["COW"]
        self.assertEqual((row["converted"], row["adjust_in"], row["adjust_out"]),
                         ("40.000", "103.000", "1.000"))  # stock_in is ADJUSTMENT_IN too
        self.assertEqual(row["closing"], "62.000")


class VisibilityTests(BalanceBase):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(
            business=self.business, supplier_code="RAM01", name="Ram", is_active=True)
        for milk_type, rate in (("COW", "38"), ("BUFFALO", "15")):
            SupplierMilkRate.objects.create(
                business=self.business, supplier=self.supplier, milk_type=milk_type,
                rate=D(rate), effective_from=ist(2026, 9, 1), created_by=self.owner)

    def collect(self, milk_type, litres):
        kw = {"fat": D("6")} if milk_type == "BUFFALO" else {}
        return create_milk_collection(
            business_id=self.business.pk, supplier_id=self.supplier.pk,
            milk_type=milk_type, collected_at=ist(2026, 10, 1, 6), litres=D(litres),
            user=self.owner, client_ref=f"c-{milk_type}", **kw)

    def test_unmapped_milk_shows_as_unposted_not_hidden(self):
        BusinessMilkConfig.objects.filter(business=self.business).update(buffalo_product=None)
        self.collect("COW", "10")
        self.collect("BUFFALO", "7")
        r = self.get({"date": "2026-10-01"})
        self.assertEqual(r.data["mapping"]["BUFFALO"]["mapped"], False)
        self.assertEqual(r.data["mapping"]["COW"]["mapped"], True)
        day = r.data["days"][0]
        self.assertEqual((day["COW"]["bought"], day["COW"]["unposted_bought_litres"]),
                         ("10.000", "0.000"))
        self.assertEqual((day["BUFFALO"]["bought"], day["BUFFALO"]["unposted_bought_litres"]),
                         ("0.000", "7.000"))
        self.assertEqual(day["TOTAL"]["unposted_bought_litres"], "7.000")

    def test_cancelled_collection_nets_out_of_both_sides(self):
        from apps.purchases.services import cancel_milk_collection
        c = self.collect("COW", "10")
        cancel_milk_collection(business_id=self.business.pk, collection_id=c.pk,
                               user=self.owner, reason="Wrong can")
        row = self.get({"date": "2026-10-01"}).data["days"][0]["COW"]
        self.assertEqual((row["bought"], row["unposted_bought_litres"]), ("0.000", "0.000"))

    def test_no_mapping_at_all(self):
        BusinessMilkConfig.objects.filter(business=self.business).delete()
        r = self.get({"date": "2026-10-01"})
        self.assertFalse(r.data["mapping"]["COW"]["mapped"])
        self.assertEqual(r.data["days"][0]["TOTAL"]["closing"], "0.000")


class ApiTests(BalanceBase):
    def test_param_rules(self):
        for params in ({}, {"date": "x"}, {"from": "2026-10-01"}, {"to": "2026-10-01"},
                       {"date": "2026-10-01", "from": "2026-10-01", "to": "2026-10-02"},
                       {"from": "2026-10-02", "to": "2026-10-01"},
                       {"from": "2026-01-01", "to": "2026-12-31"}):
            self.assertEqual(self.get(params).status_code, 400, params)
        ok = self.get({"from": "2026-08-01", "to": "2026-09-30"})  # exactly 61 days
        self.assertEqual((ok.status_code, len(ok.data["days"])), (200, 61))

    def test_roles_and_tenant(self):
        for user in (self.staff, self.outsider):
            self.assertEqual(self.get({"date": "2026-10-01"}, user=user).status_code, 403)
        self.assertEqual(self.get({"date": "2026-10-01"}, user=self.owner).status_code, 200)
        other = self.api(self.other_owner).get(self.url(), {"date": "2026-10-01"})
        self.assertEqual(other.status_code, 403)  # not a member of this business
        self.mv(M.PURCHASE_RECEIPT, 5, ist(2026, 10, 1, 6))
        other_biz = reverse("reports:milk-balance", kwargs={"business_id": self.other_business.pk})
        row = self.api(self.other_owner).get(other_biz, {"date": "2026-10-01"}).data["days"][0]
        self.assertEqual(row["COW"]["bought"], "0.000")  # sees none of ours
