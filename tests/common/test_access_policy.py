"""Cost data is OWNER/MANAGER only; ledger headers are not editable in admin."""
from django.contrib import admin
from django.urls import reverse

from apps.purchases.models import SupplierPurchase
from apps.suppliers.models import Supplier
from tests.billing.base import ist
from tests.inventory.base import InvBase


class MilkRateAccessTests(InvBase):
    def test_staff_cannot_read_supplier_milk_rates(self):
        supplier = Supplier.objects.create(
            business=self.business, supplier_code="RAM01", name="Ram", is_active=True)
        url = f"/api/v1/businesses/{self.business.pk}/suppliers/{supplier.pk}/milk-rates/"
        self.assertEqual(self.api(self.staff).get(url).status_code, 403)
        self.assertEqual(self.api(self.outsider).get(url).status_code, 403)
        self.assertEqual(self.api(self.manager).get(url).status_code, 200)
        self.assertEqual(self.api(self.owner).get(url).status_code, 200)


class AdminLedgerTests(InvBase):
    def test_purchase_header_admin_is_read_only(self):
        model_admin = admin.site._registry[SupplierPurchase]
        for check in (model_admin.has_add_permission(None),
                      model_admin.has_change_permission(None),
                      model_admin.has_delete_permission(None)):
            self.assertFalse(check)


class ClosingStockDeletedProductTests(InvBase):
    def test_deleted_product_with_stock_stays_in_the_report(self):
        from apps.products.models import ProductPrice
        self.stock_in(self.paneer, 4, at=ist(2026, 9, 10))
        ProductPrice.objects.create(
            business=self.business, product=self.paneer, price_type="PURCHASE",
            unit_price="10", effective_from=ist(2026, 1, 1))
        self.paneer.is_deleted = True
        self.paneer.save(update_fields=["is_deleted"])
        url = reverse("reports:closing-stock", kwargs={"business_id": self.business.pk})
        r = self.api(self.manager).get(url, {"month": "2026-09"})
        item = next(i for i in r.data["items"] if i["sku"] == "PNR")
        self.assertEqual((item["closing_qty"], item["product_deleted"]), ("4.000", True))
        self.assertEqual(r.data["total_closing_value"], "40.00")
