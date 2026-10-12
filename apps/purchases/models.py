
import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from apps.businesses.models import Business
from apps.common.models import SoftDeleteModel
from apps.products.models import Product
from apps.suppliers.models import Supplier


class SupplierPurchase(SoftDeleteModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        RECEIVED = "RECEIVED", "Received"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="purchases"
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name="purchases"
    )
    purchase_number = models.CharField(max_length=50)
    purchased_at = models.DateTimeField()
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    notes = models.TextField(blank=True, default="")
    total_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    received_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "purchases_supplier_purchase"
        ordering = ["-purchased_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "purchase_number"],
                name="uniq_purchase_number_business",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=0),
                name="purchase_total_nonnegative",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "status", "purchased_at"],
                name="purchase_biz_status_date_idx",
            ),
            models.Index(
                fields=["business", "supplier", "purchased_at"],
                name="purchase_biz_supplier_date_idx",
            ),
        ]

    def __str__(self):
        return f"{self.purchase_number} - {self.supplier.name}"


class PurchaseLine(SoftDeleteModel):
    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    purchase = models.ForeignKey(
        SupplierPurchase,
        on_delete=models.PROTECT,
        related_name="lines",
    )
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="purchase_lines"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="purchase_lines"
    )
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
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "purchases_purchase_line"
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="purchase_line_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0),
                name="purchase_line_price_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(line_total__gte=0),
                name="purchase_line_total_nonnegative",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "purchase"],
                name="purchline_biz_purchase_idx",
            ),
            models.Index(
                fields=["business", "product"],
                name="purchline_biz_product_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        self.line_total = (self.quantity * self.unit_price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            if {"quantity", "unit_price"} & update_fields:
                update_fields.add("line_total")
            kwargs["update_fields"] = update_fields

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"
