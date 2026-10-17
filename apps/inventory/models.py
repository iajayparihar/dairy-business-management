
import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.businesses.models import Business
from apps.common.models import SoftDeleteModel
from apps.payments.models import ImmutableLedgerQuerySet
from apps.products.models import Product


class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        PURCHASE_RECEIPT = "PURCHASE_RECEIPT", "Purchase receipt"
        DELIVERY = "DELIVERY", "Customer delivery"
        SALE = "SALE", "Shop sale"
        WASTAGE = "WASTAGE", "Wastage"
        ADJUSTMENT_IN = "ADJUSTMENT_IN", "Positive adjustment"
        ADJUSTMENT_OUT = "ADJUSTMENT_OUT", "Negative adjustment"
        PRODUCTION_IN = "PRODUCTION_IN", "Production output"
        PRODUCTION_OUT = "PRODUCTION_OUT", "Production input"
        REVERSAL_IN = "REVERSAL_IN", "Reversal (stock in)"
        REVERSAL_OUT = "REVERSAL_OUT", "Reversal (stock out)"


    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="stock_movements"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="stock_movements"
    )
    movement_type = models.CharField(
        max_length=20, choices=MovementType.choices
    )
    # Always positive. The movement type determines whether stock goes
    # in or out. Reversals use the opposite direction of the original.
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(0.001)],
    )
    occurred_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    purchase_line = models.ForeignKey(
        "purchases.PurchaseLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    delivery = models.ForeignKey(
        "deliveries.Delivery",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    sale_line = models.ForeignKey(
        "sales.SaleLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    milk_collection = models.ForeignKey(
        "purchases.MilkCollection",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    stock_adjustment = models.ForeignKey(
        "inventory.StockAdjustment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    production_line = models.ForeignKey(
        "inventory.ProductionLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    reversal_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversals",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_stock_movement"
        ordering = ["-occurred_at", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="stock_movement_quantity_positive",
            ),
            models.UniqueConstraint(
                fields=["purchase_line"],
                condition=Q(movement_type="PURCHASE_RECEIPT"),
                name="uniq_stock_purchase_receipt",
            ),
            models.UniqueConstraint(
                fields=["milk_collection"],
                condition=Q(
                    movement_type="PURCHASE_RECEIPT",
                    milk_collection__isnull=False,
                ),
                name="uniq_stock_milk_receipt",
            ),
            models.UniqueConstraint(
                fields=["delivery"],
                condition=Q(movement_type="DELIVERY"),
                name="uniq_stock_delivery_movement",
            ),
            models.UniqueConstraint(
                fields=["sale_line"],
                condition=Q(movement_type="SALE"),
                name="uniq_stock_sale_movement",
            ),
            models.UniqueConstraint(
                fields=["production_line"],
                condition=Q(movement_type__in=["PRODUCTION_IN", "PRODUCTION_OUT"]),
                name="uniq_stock_production_movement",
            ),
            models.UniqueConstraint(
                fields=["stock_adjustment"],
                condition=Q(
                    stock_adjustment__isnull=False, reversal_of__isnull=True
                ),
                name="uniq_stock_adjustment_movement",
            ),
            models.UniqueConstraint(
                fields=["reversal_of"],
                # Purchase lines may be returned in several parts; the
                # purchase adjustment service caps the total returned.
                condition=Q(reversal_of__isnull=False, purchase_line__isnull=True),
                name="uniq_stock_single_reversal",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "product", "occurred_at"],
                name="stock_biz_product_time_idx",
            ),
            models.Index(
                fields=["business", "movement_type", "occurred_at"],
                name="stock_biz_type_time_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError(
                "Stock movements are immutable. Create a reversal instead."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            "Stock movements cannot be deleted. Create a reversal instead."
        )

    def __str__(self):
        return (
            f"{self.product.name} - {self.movement_type} "
            f"- {self.quantity}"
        )


class BusinessMilkConfig(models.Model):
    """
    Which catalog product holds a business's raw cow / buffalo milk stock.
    Milk collections post stock to these products. No mapping means no
    stock is posted (and nothing is back-posted when one is added later).
    """

    business = models.OneToOneField(
        Business, on_delete=models.CASCADE, related_name="milk_config"
    )
    cow_product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    buffalo_product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inventory_business_milk_config"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(cow_product__isnull=True)
                    | Q(buffalo_product__isnull=True)
                    | ~Q(cow_product=models.F("buffalo_product"))
                ),
                name="milk_config_distinct_products",
            ),
        ]


class StockAdjustmentQuerySet(ImmutableLedgerQuerySet):
    """Adjustments change state one row at a time, via the service."""


StockAdjustmentManager = models.Manager.from_queryset(StockAdjustmentQuerySet)


class StockAdjustment(models.Model):
    """
    A wastage / manual stock correction, recorded with a reason.

    Never edited or deleted: cancel it with a reason (posts a reversal at
    the original time). WASTAGE and LOSS take stock out, GAIN puts it in.
    """

    class Kind(models.TextChoices):
        WASTAGE = "WASTAGE", "Wastage"
        GAIN = "GAIN", "Stock gain"
        LOSS = "LOSS", "Stock loss"

    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    objects = StockAdjustmentManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="stock_adjustments"
    )
    # ADJ-<YYMMDD>-<seq>, server-generated, never reused.
    adjustment_number = models.CharField(max_length=40, editable=False)
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="stock_adjustments"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    occurred_at = models.DateTimeField()
    reason = models.TextField()
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.POSTED
    )
    client_ref = models.CharField(max_length=64, null=True, blank=True)
    # Stock missing when a WASTAGE / LOSS was recorded (flagged, never blocks).
    stock_shortfall = models.DecimalField(
        max_digits=12, decimal_places=3, default=0, editable=False
    )
    # Stock missing after cancelling a GAIN that was already consumed.
    cancel_stock_shortfall = models.DecimalField(
        max_digits=12, decimal_places=3, default=0, editable=False
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
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_stock_adjustment"
        ordering = ["-occurred_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "adjustment_number"],
                name="uniq_adjustment_number",
            ),
            models.UniqueConstraint(
                fields=["business", "client_ref"],
                condition=Q(client_ref__isnull=False),
                name="uniq_adjustment_client_ref",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="adjustment_quantity_positive"
            ),
            models.CheckConstraint(
                condition=~Q(reason=""), name="adjustment_needs_reason"
            ),
            models.CheckConstraint(
                condition=Q(status="POSTED")
                | (Q(status="CANCELLED") & ~Q(cancel_reason="")),
                name="adjustment_cancel_needs_reason",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "occurred_at"], name="adj_biz_time_idx"
            ),
        ]

    def delete(self, *args, **kwargs):
        raise ValueError("Adjustments cannot be deleted. Cancel instead.")

    def __str__(self):
        return f"{self.adjustment_number} ({self.status})"


class ProductionBatchQuerySet(ImmutableLedgerQuerySet):
    """A batch changes state one row at a time, via the production service."""


ProductionBatchManager = models.Manager.from_queryset(ProductionBatchQuerySet)


class ProductionBatch(models.Model):
    """
    One conversion run: input products used up, output products made.

    Never edited or deleted: cancel it with a reason, or correct it
    (cancel + replacement at the original time).
    """

    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    objects = ProductionBatchManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="production_batches"
    )
    # PRD-<YYMMDD>-<seq>, server-generated, never reused.
    batch_number = models.CharField(max_length=40, editable=False)
    produced_at = models.DateTimeField()
    notes = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.POSTED
    )
    client_ref = models.CharField(max_length=64, null=True, blank=True)
    replaces = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replaced_by",
    )
    # Cancelling removed output that was already used: stock went negative.
    cancel_has_shortfall = models.BooleanField(default=False, editable=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    cancel_reason = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_production_batch"
        ordering = ["-produced_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "batch_number"], name="uniq_batch_number"
            ),
            models.UniqueConstraint(
                fields=["business", "client_ref"],
                condition=Q(client_ref__isnull=False),
                name="uniq_batch_client_ref",
            ),
            models.CheckConstraint(
                condition=Q(status="POSTED")
                | (Q(status="CANCELLED") & ~Q(cancel_reason="")),
                name="batch_cancel_needs_reason",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "produced_at"], name="batch_biz_time_idx"
            ),
        ]

    def delete(self, *args, **kwargs):
        raise ValueError("Batches cannot be deleted. Cancel the batch instead.")

    def __str__(self):
        return f"{self.batch_number} ({self.status})"


class ProductionLine(models.Model):
    class Kind(models.TextChoices):
        INPUT = "INPUT", "Input used"
        OUTPUT = "OUTPUT", "Output made"

    objects = models.Manager.from_queryset(ImmutableLedgerQuerySet)()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="production_lines"
    )
    batch = models.ForeignKey(
        ProductionBatch, on_delete=models.PROTECT, related_name="lines"
    )
    kind = models.CharField(max_length=6, choices=Kind.choices)
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="production_lines"
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit = models.CharField(max_length=10)
    # INPUT only: quantity used beyond recorded stock; flagged, never blocked.
    stock_shortfall = models.DecimalField(
        max_digits=12, decimal_places=3, default=0
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_production_line"
        ordering = ["batch", "kind", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "kind", "product"], name="uniq_batch_kind_product"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="production_line_quantity_positive"
            ),
            models.CheckConstraint(
                condition=Q(kind="INPUT") | Q(stock_shortfall=0),
                name="production_output_no_shortfall",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Production lines are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Production lines cannot be deleted.")
