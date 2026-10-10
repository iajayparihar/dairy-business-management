import uuid
from django.db import models

from apps.businesses.models import Business
from apps.common.models import SoftDeleteModel


class Customer(SoftDeleteModel):
    class CustomerType(models.TextChoices):
        HOUSEHOLD = "HOUSEHOLD", "Household"
        SHOP = "SHOP", "Shop"
        OTHER = "OTHER", "Other"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="customers",
    )

    customer_code = models.CharField(max_length=30)
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    address = models.TextField(blank=True, default="")

    customer_type = models.CharField(
        max_length=20,
        choices=CustomerType.choices,
        default=CustomerType.HOUSEHOLD,
    )

    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customers_customer"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "customer_code"],
                name="uniq_customer_business_code",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "is_active", "name"],
                name="cust_biz_active_name_idx",
            ),
            models.Index(
                fields=["business", "phone"],
                name="cust_biz_phone_idx",
            ),
        ]

    def __str__(self):
        return f"{self.customer_code} - {self.name}"
