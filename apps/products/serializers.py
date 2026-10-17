
from django.core.exceptions import ImproperlyConfigured
from rest_framework import serializers

from apps.products.models import Product, ProductCategory, ProductPrice


def get_request_business(context):
    request = context.get("request")
    business = getattr(request, "business", None) if request else None

    if business is None:
        raise ImproperlyConfigured(
            "An authorized business context is required."
        )

    return business


class ProductCategorySerializer(serializers.ModelSerializer):
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

    def create(self, validated_data):
        business = get_request_business(self.context)
        return ProductCategory.objects.create(
            business=business,
            **validated_data,
        )


class ProductSerializer(serializers.ModelSerializer):
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

    def create(self, validated_data):
        business = get_request_business(self.context)

        return Product.objects.create(
            business=business,
            **validated_data,
        )


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


class ProductPriceCreateSerializer(serializers.Serializer):
    price_type = serializers.ChoiceField(choices=ProductPrice.PriceType.values)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0
    )
    # Omitted means "from now". Backdating is refused by the service.
    effective_from = serializers.DateTimeField(required=False)
