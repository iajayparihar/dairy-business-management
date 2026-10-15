from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.businesses.models import Business, BusinessMembership
from apps.inventory.services import set_milk_config
from apps.products.models import Product
from apps.purchases.models import MilkCollection
from apps.purchases.services import (
    cancel_milk_collection,
    correct_milk_collection,
    create_milk_collection,
)
from apps.suppliers.models import Supplier, SupplierMilkRate

User = get_user_model()
COW = MilkCollection.MilkType.COW
BUFFALO = MilkCollection.MilkType.BUFFALO
IST = ZoneInfo("Asia/Kolkata")


def ist(day, hour=7, minute=0, second=0, month=10):
    return datetime(2026, month, day, hour, minute, second, tzinfo=IST)


class ReportFixture(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="r_owner", password="test-password",
        )
        self.staff = User.objects.create_user(
            username="r_staff", password="test-password",
        )
        self.outsider = User.objects.create_user(
            username="r_out", password="test-password",
        )
        self.business = Business.objects.create(name="R", slug="r-dairy")
        self.other = Business.objects.create(name="O", slug="o2-dairy")
        for user, role, biz in (
            (self.owner, BusinessMembership.Role.OWNER, self.business),
            (self.staff, BusinessMembership.Role.STAFF, self.business),
            (self.outsider, BusinessMembership.Role.OWNER, self.other),
        ):
            BusinessMembership.objects.create(
                business=biz, user=user, role=role, is_active=True,
            )
        self.ram = self.supplier(self.business, "RAM01")
        self.shyam = self.supplier(self.business, "SHY02")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.base = f"/api/v1/businesses/{self.business.id}/milk-collections/"

    def supplier(self, business, code):
        supplier = Supplier.objects.create(
            business=business, supplier_code=code, name=code, is_active=True,
        )
        for milk_type, rate in ((COW, "38"), (BUFFALO, "15")):
            SupplierMilkRate.objects.create(
                business=business, supplier=supplier, milk_type=milk_type,
                rate=Decimal(rate), effective_from=ist(1),
                created_by=self.owner,
            )
        return supplier

    def collect(self, supplier=None, milk_type=BUFFALO, litres="10", fat="9",
                at=None, **kw):
        data = {
            "business_id": self.business.id,
            "supplier_id": (supplier or self.ram).id,
            "milk_type": milk_type,
            "collected_at": at or ist(15),
            "litres": litres,
            "fat": None if milk_type == COW else fat,
            "user": self.owner,
        }
        data.update(kw)
        return create_milk_collection(**data)

    def summary(self, date="2026-10-15", **params):
        params["date"] = date
        return self.client.get(self.base + "daily-summary/", params)


class DailySummaryTests(ReportFixture):
    def test_empty_day(self):
        r = self.summary()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["totals"]["entries"], 0)
        self.assertEqual(r.data["totals"]["litres"], Decimal("0"))
        self.assertIsNone(r.data["totals"]["average_fat"])
        self.assertEqual(r.data["breakdown"], [])

    def test_totals_and_breakdown_by_type_and_shift(self):
        self.collect(litres="10", fat="9")                          # B morning
        self.collect(litres="30", fat="7", at=ist(15, 17))          # B evening
        self.collect(milk_type=COW, litres="20", at=ist(15, 8))     # C morning
        r = self.summary()
        t = r.data["totals"]
        self.assertEqual(t["entries"], 3)
        self.assertEqual(t["litres"], Decimal("60.000"))
        # 10x9x15 + 30x7x15 + 20x38
        self.assertEqual(t["amount"], Decimal("1350.00") + Decimal("3150.00")
                         + Decimal("760.00"))
        rows = {(b["milk_type"], b["shift"]): b for b in r.data["breakdown"]}
        self.assertEqual(set(rows), {
            ("BUFFALO", "MORNING"), ("BUFFALO", "EVENING"),
            ("COW", "MORNING"),
        })
        self.assertEqual(rows[("BUFFALO", "MORNING")]["average_fat"],
                         Decimal("9.00"))
        self.assertIsNone(rows[("COW", "MORNING")]["average_fat"])

    def test_average_fat_is_litre_weighted_buffalo_only(self):
        self.collect(litres="10", fat="9")
        self.collect(litres="30", fat="7")
        self.collect(milk_type=COW, litres="500")
        # (10x9 + 30x7) / 40 = 7.50 (a simple average of cans would be 8)
        self.assertEqual(
            self.summary().data["totals"]["average_fat"], Decimal("7.50"),
        )

    def test_cow_only_day_has_no_average_fat(self):
        self.collect(milk_type=COW, litres="20")
        self.assertIsNone(self.summary().data["totals"]["average_fat"])

    def test_cancelled_excluded_and_corrected_counted_once(self):
        gone = self.collect(litres="10")
        keep = self.collect(litres="20")
        cancel_milk_collection(
            business_id=self.business.id, collection_id=gone.id,
            user=self.owner, reason="wrong can",
        )
        correct_milk_collection(
            business_id=self.business.id, collection_id=keep.id,
            user=self.owner, reason="re-weighed", litres="25",
        )
        t = self.summary().data["totals"]
        self.assertEqual(t["entries"], 1)
        self.assertEqual(t["litres"], Decimal("25.000"))

    def test_day_is_half_open_in_business_local_time(self):
        self.collect(at=ist(14, 23, 59, 59))
        self.collect(at=ist(15, 0, 0, 0))
        self.collect(at=ist(15, 23, 59, 59))
        self.collect(at=ist(16, 0, 0, 0))
        self.assertEqual(self.summary().data["totals"]["entries"], 2)
        self.assertEqual(self.summary("2026-10-16").data["totals"]["entries"], 1)

    def test_uses_the_business_timezone_not_the_server_one(self):
        self.business.timezone = "Asia/Dubai"          # UTC+4
        self.business.save(update_fields=["timezone"])
        # 00:30 IST on the 15th = 23:00 on the 14th in Dubai.
        self.collect(at=ist(15, 0, 30))
        self.assertEqual(self.summary("2026-10-14").data["totals"]["entries"], 1)
        self.assertEqual(self.summary("2026-10-15").data["totals"]["entries"], 0)

    def test_supplier_filter_and_isolation(self):
        self.collect(self.ram)
        self.collect(self.shyam, litres="5")
        r = self.summary(supplier=str(self.shyam.id))
        self.assertEqual(r.data["totals"]["litres"], Decimal("5.000"))
        foreign = self.supplier(self.other, "FOR01")
        self.assertEqual(self.summary(supplier=str(foreign.id)).status_code, 404)
        self.assertEqual(self.summary(supplier="nope").status_code, 400)

    def test_bad_or_missing_date(self):
        self.assertEqual(self.client.get(self.base + "daily-summary/").status_code, 400)
        self.assertEqual(self.summary("15-10-2026").status_code, 400)

    def test_other_business_data_never_leaks(self):
        foreign = self.supplier(self.other, "FOR01")
        create_milk_collection(
            business_id=self.other.id, supplier_id=foreign.id,
            milk_type=BUFFALO, collected_at=ist(15), litres="99", fat="9",
            user=self.outsider,
        )
        self.assertEqual(self.summary().data["totals"]["entries"], 0)

    def test_staff_and_outsider_are_denied(self):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.summary().status_code, 403)
        self.client.force_authenticate(self.outsider)
        self.assertIn(self.summary().status_code, (403, 404))


class MilkListTests(ReportFixture):
    def test_business_list_spans_suppliers_and_paginates(self):
        for n in range(3):
            self.collect(self.ram, at=ist(15, 7, n))
        self.collect(self.shyam)
        r = self.client.get(self.base)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 4)
        r = self.client.get(self.base, {"page_size": 3})
        self.assertEqual(len(r.data["results"]), 3)
        self.assertIsNotNone(r.data["next"])

    def test_filters(self):
        a = self.collect(self.ram, at=ist(14, 7))
        b = self.collect(self.ram, at=ist(15, 17))            # evening
        c = self.collect(self.shyam, milk_type=COW, at=ist(15, 7))
        cancel_milk_collection(
            business_id=self.business.id, collection_id=a.id,
            user=self.owner, reason="wrong can",
        )

        def ids(**params):
            return {
                row["id"] for row in
                self.client.get(self.base, params).data["results"]
            }

        self.assertEqual(ids(shift="EVENING"), {str(b.id)})
        self.assertEqual(ids(milk_type="cow"), {str(c.id)})
        self.assertEqual(ids(status="CANCELLED"), {str(a.id)})
        self.assertEqual(ids(supplier=str(self.shyam.id)), {str(c.id)})
        self.assertEqual(
            ids(start_date="2026-10-15", end_date="2026-10-15"),
            {str(b.id), str(c.id)},
        )   # end_date covers the whole local day
        self.assertEqual(ids(end_date="2026-10-14"), {str(a.id)})

    def test_bad_filters_are_400(self):
        for params in (
            {"shift": "NOON"}, {"milk_type": "GOAT"}, {"status": "X"},
            {"start_date": "yesterday"}, {"supplier": "nope"},
            {"start_date": "2026-10-16", "end_date": "2026-10-15"},
        ):
            r = self.client.get(self.base, params)
            self.assertEqual(r.status_code, 400, params)

    def test_supplier_list_has_same_filters_and_pagination(self):
        self.collect(self.ram)
        self.collect(self.ram, at=ist(15, 17))
        url = (f"/api/v1/businesses/{self.business.id}/suppliers/"
               f"{self.ram.id}/milk-collections/")
        r = self.client.get(url, {"shift": "EVENING"})
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(self.client.get(url, {"shift": "X"}).status_code, 400)

    def test_never_returns_another_business(self):
        foreign = self.supplier(self.other, "FOR01")
        create_milk_collection(
            business_id=self.other.id, supplier_id=foreign.id,
            milk_type=BUFFALO, collected_at=ist(15), litres="9", fat="9",
            user=self.outsider,
        )
        self.assertEqual(self.client.get(self.base).data["count"], 0)
        self.assertEqual(
            self.client.get(self.base, {"supplier": str(foreign.id)}).data["count"],
            0,
        )

    def test_staff_cannot_read_cost_data(self):
        collection = self.collect()
        self.client.force_authenticate(self.staff)
        supplier_url = (f"/api/v1/businesses/{self.business.id}/suppliers/"
                        f"{self.ram.id}/milk-collections/")
        for url in (self.base, supplier_url, f"{supplier_url}{collection.id}/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_list_query_count_does_not_grow_with_rows(self):
        product = Product.objects.create(
            business=self.business, sku="BUFM", name="B",
            unit=Product.Unit.LITRE, track_stock=True,
        )
        set_milk_config(
            business_id=self.business.id, cow_product_id=None,
            buffalo_product_id=product.id, user=self.owner,
        )
        self.collect(at=ist(15, 7, 0))
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.base)
        for n in range(1, 8):
            self.collect(at=ist(15, 7, n))
        with CaptureQueriesContext(connection) as big:
            r = self.client.get(self.base)
        self.assertEqual(r.data["count"], 8)
        self.assertEqual(len(big), len(small))
        self.assertTrue(all(row["stock_posted"] for row in r.data["results"]))
