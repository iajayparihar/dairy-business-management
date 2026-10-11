import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from apps.businesses.models import Business
from apps.common.models import SoftDeleteModel
from apps.common.validators import validate_same_business
from apps.customers.models import Customer
from apps.products.models import Product


class Delivery(SoftDeleteModel):
    """
    A record of a product delivered to a customer on a business date.

    unit_price is the price snapshot for this delivery.
    Historical deliveries must not use the product's current price.
    """

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

    delivery_date = models.DateField()

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
        ]

    def clean(self):
        super().clean()

        validate_same_business(
            self,
            self.customer,
            self.product,
        )

    def save(self, *args, **kwargs):
        self.line_total = (
            self.quantity * self.unit_price
        ).quantize(Decimal("0.01"))

        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.customer.name} - "
            f"{self.product.name} - "
            f"{self.delivery_date}"
        )
