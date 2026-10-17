"""Malformed query-string values are a 400 everywhere, never a 500."""
from tests.billing.base import ist
from tests.inventory.base import InvBase


class QueryParamTests(InvBase):
    def base(self):
        return f"/api/v1/businesses/{self.business.pk}"

    def test_impossible_dates_are_400_on_every_date_filter(self):
        c = self.api(self.owner)
        urls = [
            "sales/?start_date=2026-13-45", "sales/summary/?date=2026-02-31",
            "milk-collections/?start_date=2026-13-45", "milk-collections/daily-summary/?date=2026-00-10",
            f"stock/{self.paneer.pk}/?from=2026-13-45", "stock/adjustments/?to=2026-02-31",
            "stock/production/?from=2026-13-45", "reports/milk-balance/?date=2026-13-45",
            "reports/milk-balance/?from=2026-13-45&to=2026-13-46",
        ]
        for url in urls:
            r = c.get(f"{self.base()}/{url}")
            self.assertEqual(r.status_code, 400, url)

    def test_routes_filters_are_validated(self):
        c = self.api(self.owner)
        for url in ("standing-orders/?customer=abc", "standing-orders/?customer=",
                    "route-runs/?date=abc", "route-runs/?date=", "route-runs/?date=2026-13-45",
                    "route-runs/?status=NOPE"):
            r = c.get(f"{self.base()}/{url}")
            self.assertEqual(r.status_code, 400, url)
        ok = c.get(f"{self.base()}/standing-orders/?customer={self.customer.pk}")
        self.assertEqual((ok.status_code, ok.data["count"]), (200, 0))
        ok = c.get(f"{self.base()}/route-runs/?date=2026-10-01&status=OPEN")
        self.assertEqual((ok.status_code, ok.data["count"]), (200, 0))


class ImpossibleTimestampTests(InvBase):
    """parse_datetime raises on well-formed but impossible values (month 13)."""

    BAD = "2026-13-45T10:00:00Z"

    def base(self):
        return f"/api/v1/businesses/{self.business.pk}"

    def test_timestamps_in_bodies_are_400(self):
        c = self.api(self.owner)
        line = [{"product_id": str(self.paneer.pk), "quantity": "1"}]
        cases = {
            "sales/": {"client_ref": "t1", "payment_mode": "CASH", "sold_at": self.BAD, "lines": line},
            "stock/adjustments/": {"client_ref": "t2", "kind": "WASTAGE", "occurred_at": self.BAD,
                                   "product_id": str(self.paneer.pk), "quantity": "1", "reason": "abc"},
            "stock/production/": {"client_ref": "t3", "produced_at": self.BAD,
                                  "inputs": [{"product_id": str(self.cow.pk), "quantity": "1"}],
                                  "outputs": line},
        }
        for path, body in cases.items():
            r = c.post(f"{self.base()}/{path}", body, format="json")
            self.assertEqual(r.status_code, 400, path)

    def test_milk_collection_timestamp_is_400(self):
        from apps.suppliers.models import Supplier
        supplier = Supplier.objects.create(
            business=self.business, supplier_code="RAM01", name="Ram", is_active=True)
        r = self.api(self.owner).post(
            f"{self.base()}/suppliers/{supplier.pk}/milk-collections/",
            {"milk_type": "COW", "litres": "10", "collected_at": self.BAD}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
