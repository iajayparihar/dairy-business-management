"""Real-thread races on the production lock order (business -> batch -> products)."""
from concurrent.futures import ThreadPoolExecutor

from apps.inventory.models import ProductionBatch, StockMovement
from apps.inventory.production import cancel_batch, correct_batch, create_batch
from apps.inventory.services import stock_balance
from tests.billing.base import D
from tests.billing.test_concurrency import RaceBase, in_thread


class ProductionRace(RaceBase):
    def setUp(self):
        super().setUp()
        self.milk = self.cow
        self.milk.track_stock = True
        self.milk.save(update_fields=["track_stock"])
        self.dahi = self.product("DAHI", "Dahi", "100.00", unit="KG")
        self.cream = self.product("CRM", "Cream", "100.00", unit="KG")
        for p in (self.dahi, self.cream):
            p.track_stock = True
            p.save(update_fields=["track_stock"])

    def side(self, *pairs):
        return [{"product_id": str(p.pk), "quantity": q} for p, q in pairs]

    def make(self, ref, inputs, outputs):
        return lambda: create_batch(
            business_id=self.business.pk, user=self.owner_(),
            inputs=inputs, outputs=outputs, client_ref=ref)

    def bal(self, p):
        return stock_balance(business_id=self.business.pk, product_id=p.pk)

    def run_parallel(self, *fns, timeout=30):
        with ThreadPoolExecutor(max_workers=len(fns)) as pool:
            futures = [pool.submit(in_thread, fn) for fn in fns]
            return [f.result(timeout=timeout) for f in futures]

    def test_parallel_creates_get_distinct_numbers(self):
        ins, outs = self.side((self.milk, "10")), self.side((self.dahi, "2"))
        results = self.run_parallel(*[self.make(f"p{i}", ins, outs) for i in range(6)])
        self.assertEqual([r[0] for r in results], ["ok"] * 6)
        numbers = list(ProductionBatch.objects.values_list("batch_number", flat=True))
        self.assertEqual(len(set(numbers)), 6)

    def test_same_client_ref_in_parallel_makes_one_batch(self):
        ins, outs = self.side((self.milk, "10")), self.side((self.dahi, "2"))
        results = self.run_parallel(*[self.make("same", ins, outs) for _ in range(4)])
        self.assertEqual([r[0] for r in results], ["ok"] * 4)
        self.assertEqual(len({r[1].pk for r in results}), 1)
        self.assertEqual(ProductionBatch.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_crossed_batches_sharing_products_do_not_deadlock(self):
        a = self.make("a", self.side((self.milk, "10"), (self.cream, "1")),
                      self.side((self.dahi, "2")))
        b = self.make("b", self.side((self.dahi, "1")),
                      self.side((self.cream, "1"), (self.milk, "1")))
        results = self.run_parallel(a, b, a, b)
        self.assertEqual([r[0] for r in results], ["ok"] * 4)
        self.assertEqual(ProductionBatch.objects.count(), 2)

    def test_cancel_versus_correct_one_wins_stock_nets_once(self):
        batch = self.make("x", self.side((self.milk, "10")), self.side((self.dahi, "2")))()
        cancel = lambda: cancel_batch(
            business_id=self.business.pk, batch_id=batch.pk, user=self.owner_(), reason="c")
        correct = lambda: correct_batch(
            business_id=self.business.pk, batch_id=batch.pk, user=self.owner_(), reason="k",
            inputs=self.side((self.milk, "10")), outputs=self.side((self.dahi, "3")))
        outcomes = self.run_parallel(cancel, correct)
        self.assertEqual(sorted(o[0] for o in outcomes).count("ok"), 1)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "CANCELLED")
        replaced = ProductionBatch.objects.filter(replaces=batch).exists()
        self.assertEqual(self.bal(self.dahi), D("3") if replaced else D("0"))
        self.assertEqual(self.bal(self.milk), D("-10") if replaced else D("0"))

    def test_crossed_corrections_do_not_deadlock(self):
        x = self.make("x", self.side((self.milk, "10")), self.side((self.dahi, "2")))()
        y = self.make("y", self.side((self.dahi, "1")), self.side((self.cream, "1")))()
        fix = lambda batch, ins, outs: lambda: correct_batch(
            business_id=self.business.pk, batch_id=batch.pk, user=self.owner_(),
            reason="k", inputs=ins, outputs=outs)
        results = self.run_parallel(
            fix(x, self.side((self.milk, "5"), (self.cream, "1")), self.side((self.dahi, "1"))),
            fix(y, self.side((self.dahi, "2")), self.side((self.milk, "1"), (self.cream, "2"))),
        )
        self.assertEqual([r[0] for r in results], ["ok"] * 2)
