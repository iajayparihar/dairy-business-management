from datetime import datetime, timezone
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ReversalBackfillMigrationTest(TransactionTestCase):
    """0004/0005: old full reversals become explicit unwind rows, money untouched."""

    def targets(self, payments_node):
        loader = MigrationExecutor(connection).loader
        nodes = [n for n in loader.graph.leaf_nodes() if n[0] != "payments"]
        return nodes + [("payments", payments_node)]

    def migrate(self, payments_node):
        targets = self.targets(payments_node)
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def tearDown(self):
        self.migrate("0005_backfill_allocation_unwinds")

    def test_backfill_forward_and_reverse(self):
        old = self.migrate("0003_supplierpayment_client_ref")
        Business = old.get_model("businesses", "Business")
        Supplier = old.get_model("suppliers", "Supplier")
        Purchase = old.get_model("purchases", "SupplierPurchase")
        Payment = old.get_model("payments", "SupplierPayment")
        Alloc = old.get_model("payments", "SupplierPaymentAllocation")
        Reversal = old.get_model("payments", "SupplierPaymentReversal")

        when = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        biz = Business.objects.create(name="Old", slug="old-rev")
        sup = Supplier.objects.create(business=biz, supplier_code="O1", name="O")

        def purchase(n):
            return Purchase.objects.create(
                business=biz, supplier=sup, purchase_number=n,
                purchased_at=when, status="RECEIVED",
                total_amount=Decimal("500.00"),
            )

        def payment(n):
            return Payment.objects.create(
                business=biz, supplier=sup, payment_number=n,
                paid_at=when, amount=Decimal("700.00"),
            )

        p1, p2 = purchase("A"), purchase("B")
        reversed_pay, live_pay = payment("R"), payment("L")
        a1 = Alloc.objects.create(business=biz, payment=reversed_pay, purchase=p1, amount=Decimal("500.00"))
        a2 = Alloc.objects.create(business=biz, payment=reversed_pay, purchase=p2, amount=Decimal("200.00"))
        Alloc.objects.create(business=biz, payment=live_pay, purchase=p2, amount=Decimal("300.00"))
        Reversal.objects.create(
            business=biz, payment=reversed_pay, amount=Decimal("700.00"),
            reason="old", reversed_at=when,
        )

        new = self.migrate("0005_backfill_allocation_unwinds")
        Unwind = new.get_model("payments", "SupplierPaymentAllocationReversal")
        rows = {u.allocation_id: u.amount for u in Unwind.objects.all()}
        self.assertEqual(rows, {a1.pk: Decimal("500.00"), a2.pk: Decimal("200.00")})

        # No money row was touched.
        NewAlloc = new.get_model("payments", "SupplierPaymentAllocation")
        self.assertEqual(
            sorted(NewAlloc.objects.values_list("amount", flat=True)),
            [Decimal("200.00"), Decimal("300.00"), Decimal("500.00")],
        )
        NewRev = new.get_model("payments", "SupplierPaymentReversal")
        self.assertEqual(NewRev.objects.get().amount, Decimal("700.00"))
        self.assertIsNone(NewRev.objects.get().client_ref)

        # Reversible: back to 0003 the unwind table is gone, data intact.
        back = self.migrate("0003_supplierpayment_client_ref")
        self.assertEqual(back.get_model("payments", "SupplierPaymentReversal").objects.count(), 1)
        self.assertNotIn(
            "payments_supplier_allocation_reversal",
            connection.introspection.table_names(),
        )
