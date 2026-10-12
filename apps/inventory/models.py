
import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.businesses.models import Business
from apps.common.models import SoftDeleteModel
from apps.products.models import Product


class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        PURCHASE_RECEIPT = "PURCHASE_RECEIPT", "Purchase receipt"
        DELIVERY = "DELIVERY", "Customer delivery"
        WASTAGE = "WASTAGE", "Wastage"
        ADJUSTMENT_IN = "ADJUSTMENT_IN", "Positive adjustment"
        ADJUSTMENT_OUT = "ADJUSTMENT_OUT", "Negative adjustment"
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
                fields=["delivery"],
                condition=Q(movement_type="DELIVERY"),
                name="uniq_stock_delivery_movement",
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
