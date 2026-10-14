
import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q, F, Func, Value
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators

from apps.businesses.models import Business
from apps.common.models import SoftDeleteModel
from apps.common.validators import validate_same_business

class ProductCategory(SoftDeleteModel):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="product_categories",
    )

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "products_category"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "name"],
                name="uniq_category_business_name",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "is_active"],
                name="prodcat_biz_active_idx",
            ),
        ]

    def __str__(self):
        return self.name


class Product(SoftDeleteModel):
    class Unit(models.TextChoices):
        LITRE = "L", "Litre"
        MILLILITRE = "ML", "Millilitre"
        KILOGRAM = "KG", "Kilogram"
        GRAM = "G", "Gram"
        PIECE = "PCS", "Piece"
        PACKET = "PACKET", "Packet"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="products",
    )

    category = models.ForeignKey(
        ProductCategory,
        on_delete=models.PROTECT,
        related_name="products",
        null=True,
        blank=True,
    )

    sku = models.CharField(max_length=50)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")

    unit = models.CharField(
        max_length=10,
        choices=Unit.choices,
        default=Unit.LITRE,
    )

    is_active = models.BooleanField(default=True)
    track_stock = models.BooleanField(default=False)
    # "Other" items: the price is typed per sale instead of looked up.
    allow_open_price = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "products_product"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "sku"],
                name="uniq_product_business_sku",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "is_active", "name"],
                name="product_biz_active_name_idx",
            ),
            models.Index(
                fields=["business", "category", "is_active"],
                name="product_biz_cat_active_idx",
            ),
        ]
    
    def clean(self):
        super().clean()
        if self.category_id:
            validate_same_business(self, self.category)

    def __str__(self):
        return f"{self.sku} - {self.name}"


class ProductPrice(SoftDeleteModel):
    class PriceType(models.TextChoices):
        SELLING = "SELLING", "Selling price"
        PURCHASE = "PURCHASE", "Purchase price"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="product_prices",
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="prices",
    )

    price_type = models.CharField(
        max_length=10,
        choices=PriceType.choices,
    )

    unit_price = models.DecimalField(
        max_digits=12,
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
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "products_product_price"
        ordering = ["-effective_from"]
        
        constraints = [
            models.CheckConstraint(
                condition=Q(unit_price__gte=0),
                name="product_price_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(effective_to__isnull=True)
                    | Q(effective_to__gt=F("effective_from"))
                ),
                name="product_price_valid_period",
            ),
            ExclusionConstraint(
                name="exclude_overlapping_product_prices",
                expressions=[
                    ("business", RangeOperators.EQUAL),
                    ("product", RangeOperators.EQUAL),
                    ("price_type", RangeOperators.EQUAL),
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
                    "product",
                    "price_type",
                    "effective_from",
                ],
                name="prodprice_lookup_idx",
            ),
        ]
        
    def clean(self):
        super().clean()
        validate_same_business(self, self.product)

    def __str__(self):
        return (
            f"{self.product.name}: {self.unit_price} "
            f"({self.get_price_type_display()})"
        )
