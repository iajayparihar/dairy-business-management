
import uuid

from django.db import models

from apps.businesses.models import Business
from apps.common.models import SoftDeleteModel
from django.conf import settings
from django.core.validators import MinValueValidator
from django.db.models import F, Func, Q, Value
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators

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

class SupplierMilkRate(models.Model):
    class MilkType(models.TextChoices):
        COW = "COW", "Cow"
        BUFFALO = "BUFFALO", "Buffalo"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="supplier_milk_rates",
    )

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="milk_rates",
    )

    milk_type = models.CharField(
        max_length=10,
        choices=MilkType.choices,
    )

    # Multiplier per litre per fat point.
    # Example: 10 litres × fat 9 × rate 15 = ₹1,350.
    rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )

    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "suppliers_milk_rate"
        ordering = ["-effective_from"]

        constraints = [
            models.CheckConstraint(
                condition=Q(rate__gte=0),
                name="supplier_milk_rate_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(effective_to__isnull=True)
                    | Q(effective_to__gt=F("effective_from"))
                ),
                name="supplier_milk_rate_valid_period",
            ),
            ExclusionConstraint(
                name="exclude_overlapping_supplier_milk_rates",
                expressions=[
                    ("business", RangeOperators.EQUAL),
                    ("supplier", RangeOperators.EQUAL),
                    ("milk_type", RangeOperators.EQUAL),
                    (
                        Func(
                            F("effective_from"),
                            F("effective_to"),
                            Value("[)"),
                            function="TSTZRANGE",
                            output_field=DateTimeRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
            ),
        ]

        indexes = [
            models.Index(
                fields=[
                    "business",
                    "supplier",
                    "milk_type",
                    "effective_from",
                ],
                name="sup_milk_rate_lookup_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.supplier.name} - {self.get_milk_type_display()} "
            f"@ {self.rate}"
        )