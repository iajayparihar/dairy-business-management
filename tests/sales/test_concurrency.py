"""Real-thread races on the sales lock order (business -> customer -> sale -> product)."""
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth import get_user_model

from apps.billing import bill_services
from apps.billing.models import CustomerBillLine
from apps.inventory.models import StockMovement
from apps.sales.models import Sale
from apps.sales.services import cancel_sale, correct_sale, create_sale
from tests.billing.base import D, ist
from tests.billing.test_concurrency import RaceBase, in_thread

User = get_user_model()


class SalesRace(RaceBase):
    def setUp(self):
        super().setUp()
        self.paneer = self.product("PNR", "Paneer", "400.00", unit="KG")
        self.curd = self.product("CRD", "Curd", "60.00", unit="KG")
        for p in (self.paneer, self.curd):
            p.track_stock = True
            p.save(update_fields=["track_stock"])

    def sell(self, lines, ref, mode="CASH", **kw):
        return lambda: create_sale(
            business_id=self.business.pk, user=self.owner_(),
            payment_mode=mode, lines=lines, client_ref=ref, **kw,
        )

    def line(self, product, qty="1"):
        return [{"product_id": product.pk, "quantity": qty}]

    def run_parallel(self, *fns, timeout=30):
        with ThreadPoolExecutor(max_workers=len(fns)) as pool:
            futures = [pool.submit(in_thread, fn) for fn in fns]
            return [f.result(timeout=timeout) for f in futures]

    def test_parallel_creates_get_distinct_numbers(self):
        results = self.run_parallel(*[
            self.sell(self.line(self.curd), f"p{i}") for i in range(6)
        ])
        self.assertEqual([r[0] for r in results], ["ok"] * 6)
        numbers = list(Sale.objects.values_list("sale_number", flat=True))
        self.assertEqual(len(set(numbers)), 6)

    def test_same_client_ref_in_parallel_makes_one_sale(self):
        results = self.run_parallel(*[
            self.sell(self.line(self.paneer), "same") for _ in range(4)
        ])
        self.assertEqual([r[0] for r in results], ["ok"] * 4)
        self.assertEqual(len({r[1].pk for r in results}), 1)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(StockMovement.objects.filter(movement_type="SALE").count(), 1)

    def test_credit_sale_cannot_slip_into_a_bill_being_frozen(self):
        self.deliver(self.cow, "2", ist(2026, 9, 1))
        bill = self.draft()
        late = self.sell(
            [{"description": "x", "amount": "5"}], "late", mode="CREDIT",
            customer_id=self.customer.pk, sold_at=ist(2026, 9, 10),
        )
        finalized, rival = self.race(
            bill_services, "build_bill_lines", self.finalize_call(bill), late,
        )
        self.assertEqual(finalized[0], "ok")
        self.assertEqual(rival[0], "rejected", "sale slipped into a frozen period")
        self.assertFalse(CustomerBillLine.objects.filter(sale_line__isnull=False).exists())
        self.assertEqual(Sale.objects.count(), 0)

    def test_cancel_versus_correct_one_wins_stock_nets_once(self):
        sale = self.sell(self.line(self.paneer), "a")()
        cancel = lambda: cancel_sale(
            business_id=self.business.pk, sale_id=sale.pk,
            user=self.owner_(), reason="c")
        correct = lambda: correct_sale(
            business_id=self.business.pk, sale_id=sale.pk, user=self.owner_(),
            reason="k", payment_mode="CASH", lines=self.line(self.paneer, "2"))
        outcomes = self.run_parallel(cancel, correct)
        self.assertEqual(sorted(o[0] for o in outcomes).count("ok"), 1)
        sale.refresh_from_db()
        self.assertEqual(sale.status, "CANCELLED")
        reversals = StockMovement.objects.filter(
            movement_type="REVERSAL_IN", sale_line__sale=sale)
        self.assertEqual(reversals.count(), 1)

    def test_crossed_corrections_do_not_deadlock(self):
        s1 = self.sell(self.line(self.paneer), "s1")()
        s2 = self.sell(self.line(self.curd), "s2")()
        fix = lambda sale, product: (lambda: correct_sale(
            business_id=self.business.pk, sale_id=sale.pk, user=self.owner_(),
            reason="swap", payment_mode="CASH", lines=self.line(product)))
        outcomes = self.run_parallel(fix(s1, self.curd), fix(s2, self.paneer))
        self.assertEqual([o[0] for o in outcomes], ["ok", "ok"])

    def test_credit_sale_versus_void(self):
        self.deliver(self.cow, "2", ist(2026, 9, 1))
        bill = self.finalize(self.draft())
        void = lambda: bill_services.void_bill(
            business_id=self.business.pk, bill_id=bill.pk, reason="redo",
            user=self.owner_())
        sale = self.sell(
            [{"description": "x", "amount": "5"}], "v", mode="CREDIT",
            customer_id=self.customer.pk, sold_at=ist(2026, 9, 10))
        outcomes = self.run_parallel(void, sale)
        # Either order is fine; what matters is no deadlock and consistency.
        voided = outcomes[0][0] == "ok"
        self.assertTrue(voided)
        if outcomes[1][0] == "ok":
            draft = bill_services.CustomerBill.objects.get(status="DRAFT")
            rows, _ = bill_services.build_bill_lines(
                self.business, self.customer.pk, draft.period_start, draft.period_end)
            self.assertEqual(sum(1 for r in rows if r["sale_line_id"]), 1)
