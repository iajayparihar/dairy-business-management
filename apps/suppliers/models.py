
import uuid

from django.db import models

from apps.businesses.models import Business
from apps.common.models import SoftDeleteModel


class Supplier(SoftDeleteModel):
    class SupplierType(models.TextChoices):
        FARMER = "FARMER", "Farmer"
        COLLECTION_CENTER = "COLLECTION_CENTER", "Collection Center"
        DISTRIBUTOR = "DISTRIBUTOR", "Distributor"
        OTHER = "OTHER", "Other"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="suppliers",
    )

    supplier_code = models.CharField(max_length=30)
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    address = models.TextField(blank=True, default="")

    supplier_type = models.CharField(
        max_length=30,
        choices=SupplierType.choices,
        default=SupplierType.FARMER,
    )

    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "suppliers_supplier"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "supplier_code"],
                name="uniq_supplier_business_code",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "is_active", "name"],
                name="supp_biz_active_name_idx",
            ),
            models.Index(
                fields=["business", "phone"],
                name="supp_biz_phone_idx",
            ),
        ]

    def __str__(self):
        return f"{self.supplier_code} - {self.name}"
