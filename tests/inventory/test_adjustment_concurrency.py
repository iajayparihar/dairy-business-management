"""Real-thread races on the adjustment lock order (business -> adjustment -> product)."""
from concurrent.futures import ThreadPoolExecutor

from apps.inventory.adjustments import cancel_adjustment, create_adjustment
from apps.inventory.models import StockAdjustment, StockMovement
from apps.inventory.services import stock_balance
from apps.sales.services import create_sale
from tests.billing.base import D
from tests.billing.test_concurrency import RaceBase, in_thread


class AdjustmentRace(RaceBase):
    def setUp(self):
        super().setUp()
        self.paneer = self.product("PNR", "Paneer", "400.00", unit="KG")
        self.paneer.track_stock = True
        self.paneer.save(update_fields=["track_stock"])

    def adjust(self, ref, kind="WASTAGE", qty="1"):
        return lambda: create_adjustment(
            business_id=self.business.pk, user=self.owner_(), kind=kind,
            product_id=self.paneer.pk, quantity=qty, reason="spoiled", client_ref=ref)

    def cancel(self, adj):
        return lambda: cancel_adjustment(
            business_id=self.business.pk, adjustment_id=adj.pk,
            user=self.owner_(), reason="mistake")

    def run_parallel(self, *fns, timeout=30):
        with ThreadPoolExecutor(max_workers=len(fns)) as pool:
            futures = [pool.submit(in_thread, fn) for fn in fns]
            return [f.result(timeout=timeout) for f in futures]

    def test_parallel_creates_get_distinct_numbers(self):
        results = self.run_parallel(*[self.adjust(f"p{i}") for i in range(6)])
        self.assertEqual([r[0] for r in results], ["ok"] * 6)
        numbers = list(StockAdjustment.objects.values_list("adjustment_number", flat=True))
        self.assertEqual(len(set(numbers)), 6)

    def test_same_client_ref_in_parallel_makes_one_adjustment(self):
        results = self.run_parallel(*[self.adjust("same") for _ in range(4)])
        self.assertEqual([r[0] for r in results], ["ok"] * 4)
        self.assertEqual(len({r[1].pk for r in results}), 1)
        self.assertEqual(StockAdjustment.objects.count(), 1)
        self.assertEqual(StockMovement.objects.filter(movement_type="WASTAGE").count(), 1)

    def test_double_cancel_in_parallel_reverses_once(self):
        adj = self.adjust("a")()
        results = self.run_parallel(self.cancel(adj), self.cancel(adj))
        self.assertEqual(sorted(r[0] for r in results), ["ok", "rejected"])
        self.assertEqual(StockMovement.objects.filter(reversal_of__isnull=False).count(), 1)
        self.assertEqual(
            stock_balance(business_id=self.business.pk, product_id=self.paneer.pk), D("0"))

    def test_adjustments_and_sales_on_one_product_never_deadlock(self):
        sale = lambda i: lambda: create_sale(
            business_id=self.business.pk, user=self.owner_(), payment_mode="CASH",
            lines=[{"product_id": self.paneer.pk, "quantity": "1"}], client_ref=f"s{i}")
        fns = [self.adjust(f"w{i}") for i in range(3)] + [sale(i) for i in range(3)]
        results = self.run_parallel(*fns)
        self.assertEqual([r[0] for r in results], ["ok"] * 6)
        self.assertEqual(
            stock_balance(business_id=self.business.pk, product_id=self.paneer.pk), D("-6"))
