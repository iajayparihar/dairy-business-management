"""The stock ledger cannot be bulk-edited or bulk-deleted around save()/delete()."""
from apps.inventory.models import StockMovement
from tests.billing.base import ist
from tests.inventory.base import D, InvBase


class StockLedgerImmutableTests(InvBase):
    def test_queryset_writes_are_blocked_and_rows_survive(self):
        move = self.stock_in(self.paneer, "5", at=ist(2026, 10, 1))
        qs = StockMovement.objects.filter(pk=move.pk)
        with self.assertRaises(ValueError):
            qs.update(quantity=D("999"))
        with self.assertRaises(ValueError):
            qs.delete()
        with self.assertRaises(ValueError):
            StockMovement.objects.bulk_update([move], ["quantity"])
        with self.assertRaises(ValueError):
            StockMovement.objects.all().delete()
        move.refresh_from_db()
        self.assertEqual(move.quantity, D("5"))

    def test_normal_posting_still_works(self):
        self.stock_in(self.paneer, "1")
        self.assertEqual(StockMovement.objects.filter(product=self.paneer).count(), 1)
