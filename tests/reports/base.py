"""Fixtures for the Phase 9 report tests (reuses the inventory business)."""
from datetime import timedelta

from apps.expenses import services as expense_services
from apps.payments.services import record_supplier_payment
from apps.purchases.services import create_milk_collection
from apps.suppliers.models import Supplier, SupplierMilkRate
from tests.billing.base import PRICE_FROM, ist  # noqa: F401
from tests.inventory.base import D, InvBase  # noqa: F401


class ReportBase(InvBase):
    """InvBase plus two suppliers with a flat cow rate of Rs.10 / litre."""

    def setUp(self):
        super().setUp()
        self.s1 = self.supplier("S1", "Ram")
        self.s2 = self.supplier("S2", "Shyam")
        self._spay = 0
        self.power = expense_services.create_category(
            business_id=self.business.pk, user=self.owner, name="Power")

    def supplier(self, code, name):
        s = Supplier.objects.create(
            business=self.business, supplier_code=code, name=name
        )
        SupplierMilkRate.objects.create(
            business=self.business, supplier=s,
            milk_type=SupplierMilkRate.MilkType.COW, rate=D("10.00"),
            effective_from=PRICE_FROM, created_by=self.owner,
        )
        return s

    def milk(self, supplier, litres, at, milk_type="COW"):
        return create_milk_collection(
            business_id=self.business.pk, supplier_id=supplier.pk,
            milk_type=milk_type, collected_at=at, litres=D(str(litres)),
            user=self.owner,
        )

    def spay(self, supplier, amount, at, method="CASH", **kw):
        self._spay += 1
        return record_supplier_payment(
            business_id=self.business.pk, supplier_id=supplier.pk,
            payment_number=f"RP-{self._spay:04d}", paid_at=at,
            amount=D(str(amount)), user=self.owner, payment_method=method,
            **kw,
        )

    def days(self, n):
        return ist(2026, 9, 1) + timedelta(days=n)

    def spend(self, amount, day, user=None):
        self._spay += 1
        return expense_services.create_expense(
            business_id=self.business.pk, user=user or self.owner,
            amount=amount, category_id=self.power.pk, expense_date=day,
            client_ref=f"exp-{self._spay}")
