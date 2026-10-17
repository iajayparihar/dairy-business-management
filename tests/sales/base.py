"""Fixtures for the shop-sales tests (reuses the billing business)."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.sales.services import create_sale
from tests.billing.base import BillFlow, ist

D = Decimal


class SalesBase(BillFlow, TestCase):
    def setUp(self):
        super().setUp()
        self.paneer = self.product("PNR", "Paneer", "400.00", unit="KG")
        self.paneer.track_stock = True
        self.paneer.save(update_fields=["track_stock"])
        self._ref = 0

    def ref(self):
        self._ref += 1
        return f"ref-{self._ref}"

    def sell(self, lines=None, mode="CASH", user=None, **kw):
        kw.setdefault("client_ref", self.ref())
        if mode == "CREDIT":
            kw.setdefault("customer_id", self.customer.pk)
        return create_sale(
            business_id=self.business.pk,
            user=user or self.staff,
            payment_mode=mode,
            lines=lines if lines is not None else [
                {"product_id": self.ghee.pk, "quantity": "0.5"}
            ],
            **kw,
        )

    def hours_ago(self, h):
        return timezone.now() - timedelta(hours=h)
