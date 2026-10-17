"""
Shop counter sales.

A Sale is a record, never edited or deleted: cancel it with a reason, or
correct it (cancel + replacement at the original time). Only CREDIT sales
reach a customer's monthly bill; CASH / ONLINE sales are shop history.
"""
import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.businesses.models import Business
from apps.common.validators import validate_same_business
from apps.customers.models import Customer
from apps.payments.models import ImmutableLedgerQuerySet
from apps.products.models import Product


def line_total_for(quantity, unit_price):
    return (quantity * unit_price).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


class SaleQuerySet(models.QuerySet):
    """A sale changes state one row at a time, through the sale service."""

    def update(self, **kwargs):
        raise ValueError("Sales cannot be bulk-updated.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("Sales cannot be bulk-updated.")

    def delete(self):
        raise ValueError("Sales cannot be deleted. Cancel the sale instead.")


class SaleLineQuerySet(ImmutableLedgerQuerySet):
    """Lines are written one at a time by the sale service."""


SaleManager = models.Manager.from_queryset(SaleQuerySet)
SaleLineManager = models.Manager.from_queryset(SaleLineQuerySet)


class Sale(models.Model):
    class PaymentMode(models.TextChoices):
        CASH = "CASH", "Cash"
        ONLINE = "ONLINE", "Online"
        CREDIT = "CREDIT", "Credit"

    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    objects = SaleManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="sales"
    )
    # SHOP-<YYMMDD>-<seq>, server-generated, never reused.
    sale_number = models.CharField(max_length=40, editable=False)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sales",
    )
    payment_mode = models.CharField(max_length=10, choices=PaymentMode.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.COMPLETED
    )
    sold_at = models.DateTimeField()
    total_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
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
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sales_sale"
        ordering = ["-sold_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "sale_number"], name="uniq_sale_number"
            ),
            models.UniqueConstraint(
                fields=["business", "client_ref"],
                condition=Q(client_ref__isnull=False),
                name="uniq_sale_client_ref",
            ),
            models.CheckConstraint(
                condition=~Q(payment_mode="CREDIT") | Q(customer__isnull=False),
                name="sale_credit_needs_customer",
            ),
            models.CheckConstraint(
                condition=Q(status="COMPLETED")
                | (Q(status="CANCELLED") & ~Q(cancel_reason="")),
                name="sale_cancel_needs_reason",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=0), name="sale_total_nonneg"
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "sold_at"], name="sale_biz_time_idx"
            ),
            models.Index(
                fields=["business", "customer", "sold_at"],
                name="sale_cust_time_idx",
            ),
        ]

    def clean(self):
        super().clean()
        validate_same_business(self, self.customer)

    def delete(self, *args, **kwargs):
        raise ValueError("Sales cannot be deleted. Cancel the sale instead.")

    def __str__(self):
        return f"{self.sale_number} ({self.status})"


class SaleLine(models.Model):
    """
    One item on a sale, as it was sold.

    product null = "other item": free description + amount, no stock, no
    price list (quantity 1, unit_price is the typed amount).
    """

    objects = SaleLineManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="sale_lines"
    )
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="lines")
    position = models.PositiveSmallIntegerField()
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sale_lines",
    )
    description = models.CharField(max_length=200)
    unit = models.CharField(max_length=10, blank=True, default="")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(
        max_digits=14, decimal_places=2, editable=False
    )
    # Quantity sold beyond recorded stock; flagged, never blocked.
    stock_shortfall = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("0.000")
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sales_sale_line"
        ordering = ["sale", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["sale", "position"], name="uniq_sale_line_position"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="sale_line_quantity_positive"
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gt=0), name="sale_line_price_positive"
            ),
            models.CheckConstraint(
                condition=Q(line_total__gt=0), name="sale_line_total_positive"
            ),
            models.CheckConstraint(
                condition=Q(product__isnull=False)
                | (Q(quantity=1) & ~Q(description="")),
                name="sale_line_other_shape",
            ),
        ]
        indexes = [
            models.Index(fields=["business", "product"], name="sline_biz_product_idx"),
        ]

    def clean(self):
        super().clean()
        validate_same_business(self, self.sale, self.product)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Sale lines are immutable.")
        self.line_total = line_total_for(self.quantity, self.unit_price)
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Sale lines cannot be deleted.")
