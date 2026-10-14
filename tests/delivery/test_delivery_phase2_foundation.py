from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.deliveries.services import create_delivery
from apps.inventory.models import StockMovement
from apps.products.models import Product, ProductPrice

User = get_user_model()
MT = StockMovement.MovementType


class Phase2FoundationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="p2_owner", password="x-pass-123")
        self.staff = User.objects.create_user(username="p2_staff", password="x-pass-123")
        self.business = create_business(name="P2 Dairy", slug="p2-dairy", owner=self.owner)
        BusinessMembership.objects.create(
            business=self.business, user=self.staff,
            role=BusinessMembership.Role.STAFF,
        )
        self.customer = Customer.objects.create(
            business=self.business, customer_code="C1", name="One",
        )
        self.milk = self._product("MILK", "Cow Milk", Product.Unit.LITRE, track_stock=True)
        self._price(self.milk, "60.00")

    def _product(self, sku, name, unit, **extra):
        return Product.objects.create(
            business=self.business, sku=sku, name=name, unit=unit, **extra,
        )

    def _price(self, product, amount):
        ProductPrice.objects.create(
            business=self.business, product=product,
            price_type=ProductPrice.PriceType.SELLING,
            unit_price=Decimal(amount),
            effective_from=timezone.now() - timedelta(days=60),
        )

    def _deliver(self, user=None, **kw):
        args = dict(
            business_id=self.business.pk, customer_id=self.customer.pk,
            product_id=self.milk.pk, quantity="2", user=user or self.owner,
        )
        args.update(kw)
        return create_delivery(**args)

    def test_delivered_at_defaults_to_now(self):
        d = self._deliver()
        self.assertLess(abs(timezone.now() - d.delivered_at), timedelta(seconds=10))

    def test_same_shift_twice_is_rejected_other_shift_allowed(self):
        self._deliver()
        with self.assertRaises(ValidationError):
            self._deliver()
        self._deliver(shift=Delivery.Shift.EVENING)
        self.assertEqual(Delivery.objects.count(), 2)

    def test_client_ref_replay_returns_same_delivery_and_one_stock_movement(self):
        first = self._deliver(client_ref="abc-1")
        again = self._deliver(client_ref="abc-1")
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Delivery.objects.count(), 1)
        self.assertEqual(StockMovement.objects.filter(movement_type=MT.DELIVERY).count(), 1)

    def test_client_ref_cannot_be_reused_for_another_customer(self):
        other = Customer.objects.create(
            business=self.business, customer_code="C2", name="Two",
        )
        self._deliver(client_ref="abc-2")
        with self.assertRaises(ValidationError):
            self._deliver(client_ref="abc-2", customer_id=other.pk)

    def test_future_timestamp_rejected(self):
        with self.assertRaises(ValidationError):
            self._deliver(delivered_at=timezone.now() + timedelta(hours=1))

    def test_staff_backdate_limited_owner_not(self):
        old = timezone.now() - timedelta(days=5)
        with self.assertRaises(ValidationError):
            self._deliver(user=self.staff, delivered_at=old)
        self._deliver(user=self.owner, delivered_at=old)

    def test_insufficient_stock_is_recorded_and_flagged(self):
        d = self._deliver(quantity="3")
        self.assertEqual(d.stock_shortfall, Decimal("3.000"))
        self.assertEqual(d.status, Delivery.Status.DELIVERED)
        self.assertTrue(StockMovement.objects.filter(delivery=d).exists())

    def test_stock_covered_has_no_shortfall(self):
        StockMovement.objects.create(
            business=self.business, product=self.milk,
            movement_type=MT.ADJUSTMENT_IN, quantity=Decimal("10"),
        )
        self.assertEqual(self._deliver(quantity="4").stock_shortfall, Decimal("0.000"))

    def test_whole_number_units_reject_fractions(self):
        pcs = self._product("PEDA", "Peda", Product.Unit.PIECE)
        self._price(pcs, "10.00")
        with self.assertRaises(ValidationError):
            self._deliver(product_id=pcs.pk, quantity="1.5")
        self.assertEqual(self._deliver(product_id=pcs.pk, quantity="2").quantity, Decimal("2.000"))

    def test_open_price_product_requires_and_uses_client_price(self):
        other = self._product("OTHER", "Other", Product.Unit.PIECE, allow_open_price=True)
        with self.assertRaises(ValidationError):
            self._deliver(product_id=other.pk, quantity="1")
        d = self._deliver(product_id=other.pk, quantity="1", unit_price="137.50")
        self.assertEqual(d.line_total, Decimal("137.50"))

    def test_normal_product_rejects_client_price(self):
        with self.assertRaises(ValidationError):
            self._deliver(unit_price="1.00")

    def test_second_reversal_of_same_movement_is_rejected(self):
        d = self._deliver()
        original = StockMovement.objects.get(delivery=d)
        make = lambda: StockMovement.objects.create(
            business=self.business, product=self.milk,
            movement_type=MT.REVERSAL_IN, quantity=original.quantity,
            reversal_of=original,
        )
        make()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make()


class DeliveryListFilterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lf_owner", password="x-pass-123")
        self.business = create_business(name="LF", slug="lf-dairy", owner=self.user)
        self.customers = [
            Customer.objects.create(business=self.business, customer_code=f"C{i}", name=f"N{i}")
            for i in range(3)
        ]
        self.milk = Product.objects.create(business=self.business, sku="M", name="Milk")
        ProductPrice.objects.create(
            business=self.business, product=self.milk,
            price_type=ProductPrice.PriceType.SELLING, unit_price=Decimal("50"),
            effective_from=timezone.now() - timedelta(days=60),
        )
        for c in self.customers:
            create_delivery(
                business_id=self.business.pk, customer_id=c.pk,
                product_id=self.milk.pk, quantity="1", user=self.user,
            )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("deliveries:delivery-list-create", kwargs={"business_id": self.business.pk})

    def test_pagination_and_customer_filter(self):
        r = self.client.get(self.url, {"page_size": 2})
        self.assertEqual((r.data["count"], len(r.data["results"])), (3, 2))
        r = self.client.get(self.url, {"customer": str(self.customers[0].pk)})
        self.assertEqual(r.data["count"], 1)

    def test_invalid_filter_is_400(self):
        self.assertEqual(self.client.get(self.url, {"customer": "nope"}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {"status": "X"}).status_code, 400)

    def test_other_business_member_cannot_list(self):
        outsider = User.objects.create_user(username="lf_out", password="x-pass-123")
        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)
