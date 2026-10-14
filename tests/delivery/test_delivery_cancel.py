from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.db.models import Q, Sum
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.deliveries.services import (
    cancel_delivery,
    correct_delivery,
    create_delivery,
)
from apps.inventory.models import StockMovement
from apps.inventory.services import STOCK_IN_TYPES, STOCK_OUT_TYPES
from apps.products.models import Product, ProductPrice
from apps.products.services import change_product_price

User = get_user_model()
MT = StockMovement.MovementType


def balance(product):
    t = StockMovement.objects.filter(product=product).aggregate(
        i=Sum("quantity", filter=Q(movement_type__in=STOCK_IN_TYPES)),
        o=Sum("quantity", filter=Q(movement_type__in=STOCK_OUT_TYPES)),
    )
    return (t["i"] or Decimal(0)) - (t["o"] or Decimal(0))


class Base:
    def make(self):
        self.owner = User.objects.create_user(username="c_owner", password="x-pass-123")
        self.manager = User.objects.create_user(username="c_mgr", password="x-pass-123")
        self.staff = User.objects.create_user(username="c_staff", password="x-pass-123")
        self.business = create_business(name="C Dairy", slug="c-dairy", owner=self.owner)
        for user, role in ((self.manager, "MANAGER"), (self.staff, "STAFF")):
            BusinessMembership.objects.create(
                business=self.business, user=user, role=role,
            )
        self.customer = Customer.objects.create(
            business=self.business, customer_code="C1", name="One",
        )
        self.milk = Product.objects.create(
            business=self.business, sku="M", name="Milk", track_stock=True,
        )
        ProductPrice.objects.create(
            business=self.business, product=self.milk,
            price_type=ProductPrice.PriceType.SELLING, unit_price=Decimal("60"),
            effective_from=timezone.now() - timedelta(days=60),
        )
        StockMovement.objects.create(
            business=self.business, product=self.milk,
            movement_type=MT.ADJUSTMENT_IN, quantity=Decimal("100"),
        )

    def deliver(self, **kw):
        args = dict(
            business_id=self.business.pk, customer_id=self.customer.pk,
            product_id=self.milk.pk, quantity="2", user=self.staff,
        )
        args.update(kw)
        return create_delivery(**args)


class CancelServiceTests(Base, TestCase):
    def setUp(self):
        self.make()

    def cancel(self, d, user=None, reason="Customer refused"):
        return cancel_delivery(
            business_id=self.business.pk, delivery_id=d.pk,
            user=user or self.owner, reason=reason,
        )

    def test_cancel_keeps_row_reverses_stock_and_records_who_and_why(self):
        d = self.deliver()
        self.assertEqual(balance(self.milk), Decimal("98"))

        self.cancel(d, user=self.manager)

        d.refresh_from_db()
        self.assertEqual(d.status, Delivery.Status.CANCELLED)
        self.assertEqual((d.cancelled_by, d.cancel_reason), (self.manager, "Customer refused"))
        self.assertIsNotNone(d.cancelled_at)
        self.assertEqual(balance(self.milk), Decimal("100"))
        original = StockMovement.objects.get(delivery=d, movement_type=MT.DELIVERY)
        reversal = StockMovement.objects.get(delivery=d, movement_type=MT.REVERSAL_IN)
        self.assertEqual(reversal.reversal_of, original)
        self.assertEqual(reversal.quantity, original.quantity)

    def test_cancel_twice_is_rejected_and_reverses_once(self):
        d = self.deliver()
        self.cancel(d)
        with self.assertRaises(ValidationError):
            self.cancel(d)
        self.assertEqual(StockMovement.objects.filter(movement_type=MT.REVERSAL_IN).count(), 1)

    def test_reason_required(self):
        d = self.deliver()
        for reason in ("", "   ", None):
            with self.assertRaises(ValidationError):
                self.cancel(d, reason=reason)
        d.refresh_from_db()
        self.assertEqual(d.status, Delivery.Status.DELIVERED)

    def test_staff_cannot_cancel(self):
        d = self.deliver()
        with self.assertRaises(Exception) as ctx:
            self.cancel(d, user=self.staff)
        self.assertEqual(type(ctx.exception).__name__, "PermissionDenied")
        d.refresh_from_db()
        self.assertEqual(d.status, Delivery.Status.DELIVERED)

    def test_cancel_untracked_product_has_no_stock_effect(self):
        self.milk.track_stock = False
        self.milk.save()
        d = self.deliver()
        self.cancel(d)
        self.assertFalse(StockMovement.objects.filter(delivery=d).exists())

    def test_cancel_frees_the_shift_slot(self):
        d = self.deliver()
        self.cancel(d)
        again = self.deliver()
        self.assertNotEqual(again.pk, d.pk)

    def test_cancelled_amount_keeps_original_price(self):
        d = self.deliver()
        change_product_price(
            business_id=self.business.pk, product_id=self.milk.pk,
            price_type=ProductPrice.PriceType.SELLING, unit_price=Decimal("65"),
            effective_from=timezone.now() + timedelta(minutes=1), user=self.owner,
        )
        self.cancel(d)
        d.refresh_from_db()
        self.assertEqual((d.unit_price, d.line_total), (Decimal("60.00"), Decimal("120.00")))


class CorrectServiceTests(Base, TestCase):
    def setUp(self):
        self.make()

    def correct(self, d, **kw):
        args = dict(
            business_id=self.business.pk, delivery_id=d.pk,
            user=self.owner, reason="Wrong litres",
        )
        args.update(kw)
        return correct_delivery(**args)

    def test_correction_replaces_at_original_time_and_price(self):
        old = self.deliver(quantity="2")
        change_product_price(
            business_id=self.business.pk, product_id=self.milk.pk,
            price_type=ProductPrice.PriceType.SELLING, unit_price=Decimal("90"),
            effective_from=timezone.now() + timedelta(minutes=1), user=self.owner,
        )

        new = self.correct(old, quantity="3")

        old.refresh_from_db()
        self.assertEqual(old.status, Delivery.Status.CANCELLED)
        self.assertTrue(old.cancel_reason.startswith("Corrected:"))
        self.assertEqual(new.replaces, old)
        self.assertEqual(new.delivered_at, old.delivered_at)
        self.assertEqual(new.unit_price, Decimal("60.00"))
        self.assertEqual(new.line_total, Decimal("180.00"))
        # Net stock effect is the corrected quantity only.
        self.assertEqual(balance(self.milk), Decimal("97"))

    def test_nothing_to_correct_is_rejected(self):
        old = self.deliver()
        with self.assertRaises(ValidationError):
            self.correct(old, quantity="2")
        old.refresh_from_db()
        self.assertEqual(old.status, Delivery.Status.DELIVERED)

    def test_invalid_correction_rolls_back_the_cancel(self):
        old = self.deliver()
        with self.assertRaises(ValidationError):
            self.correct(old, quantity="1.0005")
        old.refresh_from_db()
        self.assertEqual(old.status, Delivery.Status.DELIVERED)
        self.assertEqual(balance(self.milk), Decimal("98"))

    def test_price_cannot_be_set_on_normal_product(self):
        old = self.deliver()
        with self.assertRaises(ValidationError):
            self.correct(old, unit_price="1")

    def test_open_price_product_can_correct_price(self):
        other = Product.objects.create(
            business=self.business, sku="O", name="Other",
            unit=Product.Unit.PIECE, allow_open_price=True,
        )
        old = self.deliver(product_id=other.pk, quantity="1", unit_price="100")
        new = self.correct(old, unit_price="120")
        self.assertEqual(new.line_total, Decimal("120.00"))

    def test_shift_correction_moves_delivery(self):
        old = self.deliver()
        new = self.correct(old, shift=Delivery.Shift.EVENING)
        self.assertEqual(new.shift, Delivery.Shift.EVENING)

    def test_staff_cannot_correct(self):
        old = self.deliver()
        with self.assertRaises(Exception) as ctx:
            self.correct(old, user=self.staff, quantity="3")
        self.assertEqual(type(ctx.exception).__name__, "PermissionDenied")


class CancelAPITests(Base, TestCase):
    def setUp(self):
        self.make()
        self.client = APIClient()
        self.d = self.deliver()

    def url(self, name, business=None, d=None):
        return reverse(
            f"deliveries:{name}",
            kwargs={"business_id": (business or self.business).pk, "pk": (d or self.d).pk},
        )

    def test_owner_cancels_and_detail_shows_history(self):
        self.client.force_authenticate(self.owner)
        r = self.client.post(self.url("delivery-cancel"), {"reason": "Absent"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "CANCELLED")

        r = self.client.get(self.url("delivery-detail"))
        self.assertEqual(r.status_code, 200)
        types = [m["movement_type"] for m in r.data["stock_movements"]]
        self.assertEqual(types, ["DELIVERY", "REVERSAL_IN"])

    def test_cancel_without_reason_is_400(self):
        self.client.force_authenticate(self.owner)
        r = self.client.post(self.url("delivery-cancel"), {}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_staff_can_view_but_not_cancel_or_patch(self):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(self.url("delivery-detail")).status_code, 200)
        r = self.client.post(self.url("delivery-cancel"), {"reason": "x"}, format="json")
        self.assertEqual(r.status_code, 403)
        r = self.client.patch(self.url("delivery-detail"), {"reason": "x", "quantity": "5"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_patch_corrects_and_links_replacement(self):
        self.client.force_authenticate(self.manager)
        r = self.client.patch(
            self.url("delivery-detail"),
            {"reason": "Wrong litres", "quantity": "3.5"},
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Decimal(r.data["quantity"]), Decimal("3.500"))
        self.assertEqual(str(r.data["replaces"]), str(self.d.pk))
        old = self.client.get(self.url("delivery-detail")).data
        self.assertEqual(str(old["replaced_by"]), str(r.data["id"]))

    def test_cancelled_delivery_stays_in_list_and_filters(self):
        self.client.force_authenticate(self.owner)
        self.client.post(self.url("delivery-cancel"), {"reason": "x"}, format="json")
        list_url = reverse("deliveries:delivery-list-create", kwargs={"business_id": self.business.pk})
        self.assertEqual(self.client.get(list_url).data["count"], 1)
        self.assertEqual(self.client.get(list_url, {"status": "DELIVERED"}).data["count"], 0)

    def test_other_business_delivery_is_404_through_own_url(self):
        other_owner = User.objects.create_user(username="c_other", password="x-pass-123")
        other = create_business(name="Other", slug="other-dairy", owner=other_owner)
        self.client.force_authenticate(other_owner)
        for call in (
            lambda: self.client.get(self.url("delivery-detail", business=other)),
            lambda: self.client.post(self.url("delivery-cancel", business=other), {"reason": "x"}, format="json"),
            lambda: self.client.patch(self.url("delivery-detail", business=other), {"reason": "x", "quantity": "1"}, format="json"),
        ):
            self.assertEqual(call().status_code, 404)
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, Delivery.Status.DELIVERED)

    def test_outsider_gets_403_on_foreign_business_url(self):
        outsider = User.objects.create_user(username="c_out", password="x-pass-123")
        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get(self.url("delivery-detail")).status_code, 403)
        r = self.client.post(self.url("delivery-cancel"), {"reason": "x"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_unknown_delivery_is_404(self):
        self.client.force_authenticate(self.owner)
        ghost = Delivery(pk="00000000-0000-0000-0000-000000000001")
        r = self.client.post(self.url("delivery-cancel", d=ghost), {"reason": "x"}, format="json")
        self.assertEqual(r.status_code, 404)


class CancelConcurrencyTests(Base, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.make()

    def test_simultaneous_cancels_reverse_stock_once(self):
        d = self.deliver()
        barrier = Barrier(2)

        def attempt(_):
            close_old_connections()
            try:
                user = User.objects.get(pk=self.owner.pk)
                barrier.wait(timeout=10)
                cancel_delivery(
                    business_id=self.business.pk, delivery_id=d.pk,
                    user=user, reason="race",
                )
                return "ok"
            except ValidationError:
                return "rejected"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))

        self.assertEqual(sorted(results), ["ok", "rejected"])
        self.assertEqual(StockMovement.objects.filter(movement_type=MT.REVERSAL_IN).count(), 1)
        self.assertEqual(balance(self.milk), Decimal("100"))
