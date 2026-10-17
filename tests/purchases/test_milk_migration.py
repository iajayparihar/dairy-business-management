from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class MilkBackfillMigrationTest(TransactionTestCase):
    """Rows written before shift/pricing_basis/status keep their meaning."""

    before = [
        ("purchases", "0005_purchaseadjustment_purchaseadjustmentline_and_more"),
        ("inventory", "0005_remove_stockmovement_uniq_stock_single_reversal_and_more"),
    ]
    after = [
        ("purchases", "0008_milkcollection_cancel_replaces"),
        ("inventory", "0006_milk_stock_and_config"),
    ]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def tearDown(self):
        # Leave the test database fully migrated (all latest migrations, not
        # just `after`) so later tests see every column.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_old_rows_are_backfilled_without_touching_money(self):
        old = self.migrate(self.before)
        Business = old.get_model("businesses", "Business")
        Supplier = old.get_model("suppliers", "Supplier")
        Rate = old.get_model("suppliers", "SupplierMilkRate")
        Purchase = old.get_model("purchases", "SupplierPurchase")
        Milk = old.get_model("purchases", "MilkCollection")
        from datetime import datetime, timezone
        from decimal import Decimal

        when = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        business = Business.objects.create(name="Old", slug="old-dairy")
        supplier = Supplier.objects.create(
            business=business, supplier_code="OLD1", name="Old",
        )
        rate = Rate.objects.create(
            business=business, supplier=supplier, milk_type="COW",
            rate=Decimal("15.00"), effective_from=when,
        )
        purchase = Purchase.objects.create(
            business=business, supplier=supplier, purchase_number="MILK-OLD",
            purchased_at=when, status="RECEIVED",
            total_amount=Decimal("1350.00"),
        )
        row = Milk.objects.create(
            business=business, supplier=supplier, rate_record=rate,
            collection_number="OLD", milk_type="COW", collected_at=when,
            litres=Decimal("10"), fat=Decimal("9"),
            applied_rate=Decimal("15.00"), amount=Decimal("1350.00"),
            purchase=purchase,
        )

        new = self.migrate(self.after)
        migrated = new.get_model("purchases", "MilkCollection").objects.get(
            pk=row.pk,
        )
        # A fat-priced cow row stays fat-priced; its amount is untouched.
        self.assertEqual(migrated.pricing_basis, "FAT")
        self.assertEqual(migrated.shift, "MORNING")
        self.assertEqual(migrated.status, "RECORDED")
        self.assertIsNone(migrated.client_ref)
        self.assertIsNone(migrated.replaces_id)
        self.assertEqual(migrated.amount, Decimal("1350.00"))
        self.assertEqual(migrated.fat, Decimal("9"))

        # And the migration is reversible.
        back = self.migrate(self.before)
        self.assertTrue(
            back.get_model("purchases", "MilkCollection")
            .objects.filter(pk=row.pk).exists()
        )
