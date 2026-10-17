"""Fixtures for the Phase 7 stock tests (reuses the sales/billing business)."""
from rest_framework.test import APIClient

from apps.inventory.models import StockMovement
from tests.sales.base import D, SalesBase  # noqa: F401


class InvBase(SalesBase):
    def setUp(self):
        super().setUp()
        self.c = APIClient()
        for milk in (self.cow, self.buffalo):  # mapped in BillFlow, untracked
            milk.track_stock = True
            milk.save(update_fields=["track_stock"])

    def api(self, user=None):
        self.c.force_authenticate(user or self.staff)
        return self.c

    def stock_in(self, product, qty, at=None, kind="ADJUSTMENT_IN"):
        kw = {"occurred_at": at} if at else {}
        return StockMovement.objects.create(
            business=self.business, product=product, movement_type=kind,
            quantity=D(str(qty)), **kw)

    def tracked(self, sku, name, unit="KG", business=None):
        p = self.product(sku, name, "100.00", unit=unit, business=business)
        p.track_stock = True
        p.save(update_fields=["track_stock"])
        return p
