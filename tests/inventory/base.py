"""Fixtures for the Phase 7 stock tests (reuses the sales/billing business)."""
from rest_framework.test import APIClient

from apps.inventory.models import StockMovement
from tests.sales.base import D, SalesBase  # noqa: F401


class InvBase(SalesBase):
    def setUp(self):
        super().setUp()
        self.c = APIClient()

    def api(self, user=None):
        self.c.force_authenticate(user or self.staff)
        return self.c

    def stock_in(self, product, qty, at=None, kind="ADJUSTMENT_IN"):
        kw = {"occurred_at": at} if at else {}
        return StockMovement.objects.create(
            business=self.business, product=product, movement_type=kind,
            quantity=D(str(qty)), **kw)
