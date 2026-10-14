
import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.businesses.models import Business
from apps.common.models import (
    ActiveManager,
    AllObjectsManager,
    SoftDeleteModel,
    SoftDeleteQuerySet,
)
from apps.common.validators import validate_same_business
from apps.customers.models import Customer
from apps.products.models import Product


class DeliveryQuerySet(SoftDeleteQuerySet):
    def delete(self):
        raise ValueError(
            "Deliveries cannot be deleted. Cancel the delivery instead."
        )


class Delivery(SoftDeleteModel):
    """
    One product delivered to one customer in one shift.

    delivered_at is the exact timestamp; delivery_date is the derived
    business date (Asia/Kolkata) used for uniqueness and filtering.

    unit_price is the historical price snapshot for this delivery.
    Deliveries are never deleted: cancel them (status=CANCELLED) so
    the stock reversal and the reason stay on record.
    """

    objects = ActiveManager.from_queryset(DeliveryQuerySet)()
    all_objects = AllObjectsManager.from_queryset(DeliveryQuerySet)()

    class Shift(models.TextChoices):
        MORNING = "MORNING", "Morning"
        EVENING = "EVENING", "Evening"

    class Status(models.TextChoices):
        DELIVERED = "DELIVERED", "Delivered"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )

    # Derived from delivered_at in save().
    delivery_date = models.DateField(editable=False)

    delivered_at = models.DateTimeField(
        help_text="Exact delivery date and time, interpreted in Asia/Kolkata.",
    )

    shift = models.CharField(
        max_length=10,
        choices=Shift.choices,
        default=Shift.MORNING,
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DELIVERED,
    )

    # Quantity delivered beyond recorded stock; flagged for reconciliation.
    stock_shortfall = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=Decimal("0.000"),
        editable=False,
    )

    # Client-generated key making retried submissions safe.
    client_ref = models.CharField(max_length=64, null=True, blank=True)

    replaces = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replaced_by",
    )

    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    cancel_reason = models.TextField(blank=True, default="")

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )

    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    line_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        editable=False,
    )

    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_deliveries",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "deliveries_delivery"
        ordering = ["-delivery_date", "-created_at"]

        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="delivery_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0),
                name="delivery_price_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(line_total__gte=0),
                name="delivery_total_nonnegative",
            ),
            models.UniqueConstraint(
                fields=[
                    "business",
                    "customer",
                    "product",
                    "delivery_date",
                    "shift",
                ],
                condition=Q(status="DELIVERED"),
                name="uniq_delivery_per_shift",
            ),
            models.UniqueConstraint(
                fields=["business", "client_ref"],
                condition=Q(client_ref__isnull=False),
                name="uniq_delivery_client_ref",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="DELIVERED")
                    | (Q(status="CANCELLED") & ~Q(cancel_reason=""))
                ),
                name="delivery_cancel_needs_reason",
            ),
        ]

        indexes = [
            models.Index(
                fields=["business", "delivery_date"],
                name="delivery_biz_date_idx",
            ),
            models.Index(
                fields=["business", "customer", "delivery_date"],
                name="delivery_customer_date_idx",
            ),
            models.Index(
                fields=["business", "delivered_at"],
                name="delivery_biz_time_idx",
            ),
            models.Index(
                fields=["business", "customer", "delivered_at"],
                name="delivery_cust_time_idx",
            ),
        ]

    def clean(self):
        super().clean()

        validate_same_business(
            self,
            self.customer,
            self.product,
        )

    def delete(self, *args, **kwargs):
        raise ValueError(
            "Deliveries cannot be deleted. Cancel the delivery instead."
        )

    def soft_delete(self, user=None):
        self.delete()

    def save(self, *args, **kwargs):
        self.delivery_date = timezone.localtime(self.delivered_at).date()

        self.line_total = (
            self.quantity * self.unit_price
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        # Ensure the calculated total is persisted when using update_fields.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)

            if {"quantity", "unit_price"} & update_fields:
                update_fields.add("line_total")

            if "delivered_at" in update_fields:
                update_fields.add("delivery_date")

            if update_fields:
                kwargs["update_fields"] = update_fields

        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.customer.name} - "
            f"{self.product.name} - "
            f"{self.delivered_at}"
        )
