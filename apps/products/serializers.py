
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from rest_framework import serializers

from apps.common.serializers import RejectUnknownFieldsMixin, StrictDateTimeField
from apps.products.guards import product_has_records

from apps.products.models import Product, ProductCategory, ProductPrice


class _BusinessMixin:
    def _business(self):
        if self.instance:
            return self.instance.business
        request = self.context.get("request")
        return getattr(request, "business", None) if request else None


def get_request_business(context):
    request = context.get("request")
    business = getattr(request, "business", None) if request else None

    if business is None:
        raise ImproperlyConfigured(
            "An authorized business context is required."
        )

    return business


class ProductCategorySerializer(_BusinessMixin, RejectUnknownFieldsMixin, serializers.ModelSerializer):
    business = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = ProductCategory
        fields = [
            "id",
            "business",
            "name",
            "description",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "business",
            "created_at",
            "updated_at",
        ]

    def validate_name(self, value):
        business = self._business()
        if business is None:  # create() raises for a missing business context
            return value
        clash = ProductCategory.all_objects.filter(business=business, name__iexact=value.strip())
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("A category with this name already exists.")
        return value

    def create(self, validated_data):
        business = get_request_business(self.context)
        try:
            return ProductCategory.objects.create(business=business, **validated_data)
        except IntegrityError:  # parallel create of the same name
            raise serializers.ValidationError({"name": "A category with this name already exists."})


class ProductSerializer(_BusinessMixin, RejectUnknownFieldsMixin, serializers.ModelSerializer):
    business = serializers.PrimaryKeyRelatedField(read_only=True)

    category = serializers.PrimaryKeyRelatedField(
        queryset=ProductCategory.objects.none(),
        allow_null=True,
        required=False,
    )

    class Meta:
        model = Product
        fields = [
            "id",
            "business",
            "category",
            "sku",
            "name",
            "description",
            "unit",
            "is_active",
            "track_stock",
            "allow_open_price",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "business",
            "created_at",
            "updated_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        request = self.context.get("request")
        business = getattr(request, "business", None) if request else None

        if business is not None:
            self.fields["category"].queryset = (
                ProductCategory.objects.filter(
                    business=business,
                    is_active=True,
                )
            )

    def validate_sku(self, value):
        value = value.strip()
        business = self._business()
        if business is None:  # create() raises for a missing business context
            return value.strip()
        clash = Product.all_objects.filter(business=business, sku=value)
        if self.instance:
            if value == self.instance.sku:
                return value
            if product_has_records(self.instance):
                raise serializers.ValidationError(
                    "The SKU cannot change once the product has sales, stock or purchases."
                )
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("This SKU is already used.")
        return value

    def validate(self, attrs):
        # Stock is counted in the product's unit and only for tracked products:
        # changing either after stock rows exist would corrupt the balance.
        if self.instance:
            changed = [
                f for f in ("unit", "track_stock")
                if f in attrs and attrs[f] != getattr(self.instance, f)
            ]
            if changed and product_has_records(self.instance):
                raise serializers.ValidationError({
                    f: "Cannot change once the product has sales, stock or purchases."
                    for f in changed
                })
        return attrs

    def create(self, validated_data):
        business = get_request_business(self.context)
        try:
            return Product.objects.create(business=business, **validated_data)
        except IntegrityError:  # parallel create of the same SKU
            raise serializers.ValidationError({"sku": "This SKU is already used."})


class ProductPriceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductPrice
        fields = [
            "id",
            "price_type",
            "unit_price",
            "effective_from",
            "effective_to",
            "created_at",
        ]
        read_only_fields = fields


class ProductPriceCreateSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    price_type = serializers.ChoiceField(choices=ProductPrice.PriceType.values)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0
    )
    # Omitted means "from now". Backdating is refused by the service.
    effective_from = StrictDateTimeField(required=False)
