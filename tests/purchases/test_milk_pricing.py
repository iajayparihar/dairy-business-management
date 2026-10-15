from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import Business, BusinessMembership
from apps.purchases.models import MilkCollection
from apps.purchases.services import create_milk_collection
from apps.suppliers.models import Supplier, SupplierMilkRate

User = get_user_model()
COW = MilkCollection.MilkType.COW
BUFFALO = MilkCollection.MilkType.BUFFALO


class PricingFixture(TestCase):
    """Cow = litres x rate (flat, no fat). Buffalo = litres x fat x rate."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username="pricing_owner", password="test-password",
        )
        cls.business = Business.objects.create(
            name="Pricing Dairy", slug="pricing-dairy",
        )
        BusinessMembership.objects.create(
            business=cls.business, user=cls.owner,
            role=BusinessMembership.Role.OWNER, is_active=True,
        )
        cls.supplier = Supplier.objects.create(
            business=cls.business, supplier_code="PR01",
            name="Pricing Supplier", is_active=True,
        )

    def setUp(self):
        self.start = timezone.now().replace(microsecond=0)
        for milk_type, rate in ((COW, "38.00"), (BUFFALO, "15.00")):
            SupplierMilkRate.objects.create(
                business=self.business, supplier=self.supplier,
                milk_type=milk_type, rate=Decimal(rate),
                effective_from=self.start - timedelta(hours=1),
                created_by=self.owner,
            )
        self.seq = 0

    def create(self, **overrides):
        self.seq += 1
        data = {
            "business_id": self.business.id,
            "supplier_id": self.supplier.id,
            "milk_type": COW,
            "collected_at": self.start,
            "litres": "10",
            "user": self.owner,
        }
        data.update(overrides)
        return create_milk_collection(**data)


class MilkPricingTests(PricingFixture):
    def test_cow_is_flat_per_litre_and_stores_zero_fat(self):
        c = self.create()
        self.assertEqual(c.amount, Decimal("380.00"))
        self.assertEqual(c.pricing_basis, "FLAT")
        self.assertEqual(c.fat, Decimal("0"))
        self.assertEqual(c.purchase.total_amount, Decimal("380.00"))

    def test_buffalo_is_litres_x_fat_x_rate(self):
        c = self.create(milk_type=BUFFALO, fat="9")
        self.assertEqual(c.amount, Decimal("1350.00"))
        self.assertEqual(c.pricing_basis, "FAT")

    def test_cow_with_any_fat_is_rejected_even_zero(self):
        for fat in ("0", "9", 0):
            with self.assertRaises(ValidationError):
                self.create(fat=fat)
        self.assertEqual(MilkCollection.objects.count(), 0)

    def test_buffalo_requires_positive_fat(self):
        for fat in (None, "0", "-1"):
            with self.assertRaises(ValidationError):
                self.create(milk_type=BUFFALO, fat=fat)
        self.assertEqual(MilkCollection.objects.count(), 0)

    def test_existing_rows_are_backfilled_as_fat_basis(self):
        # Model default is what the migration applies to old rows.
        field = MilkCollection._meta.get_field("pricing_basis")
        self.assertEqual(field.default, MilkCollection.PricingBasis.FAT)


class MilkPricingAPITests(PricingFixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.url = (
            f"/api/v1/businesses/{self.business.id}/suppliers/"
            f"{self.supplier.id}/milk-collections/"
        )

    def post(self, **body):
        self.seq += 1
        payload = {
            "collected_at": self.start.isoformat(),
            "litres": "10",
        }
        payload.update(body)
        return self.client.post(self.url, payload, format="json")

    def test_api_cow_without_fat(self):
        r = self.post(milk_type="COW")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["amount"], "380.00")
        self.assertEqual(r.data["pricing_basis"], "FLAT")

    def test_api_cow_with_fat_key_is_400(self):
        for fat in ("0", "9"):
            r = self.post(milk_type="COW", fat=fat)
            self.assertEqual(r.status_code, 400)
            self.assertIn("fat", r.data)

    def test_api_buffalo_requires_fat(self):
        self.assertEqual(self.post(milk_type="BUFFALO").status_code, 400)
        self.assertEqual(
            self.post(milk_type="BUFFALO", fat="0").status_code, 400,
        )
        r = self.post(milk_type="BUFFALO", fat="9")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["amount"], "1350.00")
