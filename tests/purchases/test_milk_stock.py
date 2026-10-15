from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.test import TestCase
from rest_framework.test import APIClient

from apps.businesses.models import Business, BusinessMembership
from apps.inventory.models import BusinessMilkConfig, StockMovement
from apps.inventory.services import set_milk_config
from apps.products.models import Product
from apps.purchases.models import MilkCollection
from apps.purchases.services import create_milk_collection
from apps.suppliers.models import Supplier, SupplierMilkRate

User = get_user_model()
COW = MilkCollection.MilkType.COW
BUFFALO = MilkCollection.MilkType.BUFFALO
IST = ZoneInfo("Asia/Kolkata")
WHEN = datetime(2026, 10, 15, 7, 0, tzinfo=IST)


def make_product(business, sku, **kw):
    data = {
        "name": sku, "unit": Product.Unit.LITRE, "is_active": True,
        "track_stock": True,
    }
    data.update(kw)
    return Product.objects.create(business=business, sku=sku, **data)


class MilkStockTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="stock_owner", password="test-password",
        )
        self.staff = User.objects.create_user(
            username="stock_staff", password="test-password",
        )
        self.outsider = User.objects.create_user(
            username="stock_outsider", password="test-password",
        )
        self.business = Business.objects.create(name="S", slug="s-dairy")
        self.other_business = Business.objects.create(name="O", slug="o-dairy")
        for user, role in (
            (self.owner, BusinessMembership.Role.OWNER),
            (self.staff, BusinessMembership.Role.STAFF),
        ):
            BusinessMembership.objects.create(
                business=self.business, user=user, role=role, is_active=True,
            )
        BusinessMembership.objects.create(
            business=self.other_business, user=self.outsider,
            role=BusinessMembership.Role.OWNER, is_active=True,
        )
        self.supplier = Supplier.objects.create(
            business=self.business, supplier_code="RAM01", name="Ram",
            is_active=True,
        )
        for milk_type, rate in ((COW, "38"), (BUFFALO, "15")):
            SupplierMilkRate.objects.create(
                business=self.business, supplier=self.supplier,
                milk_type=milk_type, rate=Decimal(rate),
                effective_from=datetime(2026, 10, 1, tzinfo=IST),
                created_by=self.owner,
            )
        self.cow_p = make_product(self.business, "COWM")
        self.buf_p = make_product(self.business, "BUFM")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.base = f"/api/v1/businesses/{self.business.id}"

    def map(self, cow=True, buffalo=True):
        return set_milk_config(
            business_id=self.business.id,
            cow_product_id=self.cow_p.id if cow else None,
            buffalo_product_id=self.buf_p.id if buffalo else None,
            user=self.owner,
        )

    def collect(self, milk_type=BUFFALO, litres="10", **kw):
        data = {
            "business_id": self.business.id,
            "supplier_id": self.supplier.id,
            "milk_type": milk_type,
            "collected_at": WHEN,
            "litres": litres,
            "fat": "9" if milk_type == BUFFALO else None,
            "user": self.owner,
        }
        data.update(kw)
        return create_milk_collection(**data)

    def stock(self, product):
        return StockMovement.objects.filter(product=product).aggregate(
            t=Sum("quantity"),
        )["t"]

    # ---- posting ----

    def test_mapped_collection_posts_receipt_to_the_right_product(self):
        self.map()
        buf = self.collect(BUFFALO, "12.500")
        cow = self.collect(COW, "7")
        move = StockMovement.objects.get(milk_collection=buf)
        self.assertEqual(move.product, self.buf_p)
        self.assertEqual(move.movement_type, "PURCHASE_RECEIPT")
        self.assertEqual(move.quantity, Decimal("12.500"))
        self.assertEqual(move.occurred_at, WHEN)
        self.assertEqual(
            StockMovement.objects.get(milk_collection=cow).product,
            self.cow_p,
        )
        self.assertEqual(self.stock(self.buf_p), Decimal("12.500"))

    def test_no_mapping_still_succeeds_without_stock(self):
        c = self.collect()
        self.assertEqual(c.amount, Decimal("1350.00"))
        self.assertFalse(StockMovement.objects.exists())

    def test_mapping_added_later_does_not_back_post(self):
        self.collect()
        self.map()
        self.assertFalse(StockMovement.objects.exists())
        self.collect()
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_partial_mapping_posts_only_mapped_type(self):
        self.map(cow=False)
        self.collect(COW, "5")
        self.collect(BUFFALO, "5")
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_product_later_untracked_means_no_posting_not_failure(self):
        self.map()
        Product.objects.filter(pk=self.buf_p.pk).update(track_stock=False)
        self.collect()
        self.assertFalse(StockMovement.objects.exists())

    def test_replayed_request_does_not_post_twice(self):
        self.map()
        self.collect(client_ref="r1")
        self.collect(client_ref="r1")
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_one_receipt_per_collection_is_a_db_rule(self):
        self.map()
        c = self.collect()
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockMovement.objects.create(
                business=self.business, product=self.buf_p,
                movement_type="PURCHASE_RECEIPT", quantity=Decimal("1"),
                milk_collection=c,
            )

    # ---- mapping validation ----

    def test_mapping_rejects_bad_products(self):
        other = make_product(self.other_business, "OTHER")
        untracked = make_product(self.business, "UNT", track_stock=False)
        kilo = make_product(self.business, "KG", unit=Product.Unit.KILOGRAM)
        inactive = make_product(self.business, "OFF", is_active=False)
        for bad in (other, untracked, kilo, inactive):
            with self.assertRaises(ValidationError, msg=bad.sku):
                set_milk_config(
                    business_id=self.business.id,
                    cow_product_id=bad.id, buffalo_product_id=None,
                    user=self.owner,
                )
        with self.assertRaises(ValidationError):
            set_milk_config(
                business_id=self.business.id,
                cow_product_id=self.cow_p.id,
                buffalo_product_id=self.cow_p.id, user=self.owner,
            )
        self.assertFalse(BusinessMilkConfig.objects.exists())

    def test_db_blocks_same_product_for_both(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            BusinessMilkConfig.objects.create(
                business=self.business,
                cow_product=self.cow_p, buffalo_product=self.cow_p,
            )

    # ---- API ----

    def test_config_api_get_put(self):
        r = self.client.get(f"{self.base}/milk-config/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, {"cow_product": None, "buffalo_product": None})
        r = self.client.put(
            f"{self.base}/milk-config/",
            {"cow_product": str(self.cow_p.id),
             "buffalo_product": str(self.buf_p.id)},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(str(r.data["cow_product"]), str(self.cow_p.id))
        bad = self.client.put(
            f"{self.base}/milk-config/",
            {"cow_product": str(make_product(
                self.other_business, "Z").id), "buffalo_product": None},
            format="json",
        )
        self.assertEqual(bad.status_code, 400)

    def test_config_api_permissions(self):
        url = f"{self.base}/milk-config/"
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(
            self.client.put(url, {"cow_product": None,
                                  "buffalo_product": None},
                            format="json").status_code, 403,
        )
        self.client.force_authenticate(self.outsider)
        self.assertIn(self.client.get(url).status_code, (403, 404))

    def test_stock_posted_flag_in_api(self):
        url = (f"{self.base}/suppliers/{self.supplier.id}/milk-collections/")
        body = {
            "milk_type": "BUFFALO", "litres": "10", "fat": "9",
            "collected_at": WHEN.isoformat(),
        }
        r = self.client.post(url, body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIs(r.data["stock_posted"], False)
        self.map()
        r = self.client.post(url, body, format="json")
        self.assertIs(r.data["stock_posted"], True)
        listed = self.client.get(url).data
        rows = listed["results"] if isinstance(listed, dict) else listed
        self.assertEqual(
            sorted(row["stock_posted"] for row in rows), [False, True],
        )


class SeedMappingTests(TestCase):
    def test_seed_creates_buffalo_product_and_mapping(self):
        call_command("seed_dairy_data", verbosity=0)
        for business in Business.objects.filter(
            slug__in=["abhigyan-dairy", "vij-dairy"],
        ):
            config = business.milk_config
            self.assertEqual(config.cow_product.sku, "MILK")
            self.assertEqual(config.buffalo_product.sku, "MILK-BUF")
            self.assertTrue(config.buffalo_product.track_stock)
