
from decimal import Decimal

from rest_framework import serializers

from apps.deliveries.models import Delivery


class DeliveryCreateSerializer(serializers.Serializer):
    """Validate input required to create a delivery."""

    customer_id = serializers.UUIDField()
    product_id = serializers.UUIDField()

    delivered_at = serializers.DateTimeField()

    quantity = serializers.DecimalField(
        max_digits=12,
        decimal_places=3,
        min_value=Decimal("0.001"),
    )

    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
    )

    def validate_delivered_at(self, value):
        """Require a timezone-aware delivery timestamp."""
        from django.utils import timezone

        if timezone.is_naive(value):
            raise serializers.ValidationError(
                "The delivery timestamp must include a timezone."
            )

        return value


class DeliveryReadSerializer(serializers.ModelSerializer):
    """Serialize saved delivery records for API responses."""

    customer_name = serializers.CharField(
        source="customer.name",
        read_only=True,
    )

    product_name = serializers.CharField(
        source="product.name",
        read_only=True,
    )

    class Meta:
        model = Delivery
        fields = [
            "id",
            "business",
            "customer",
            "customer_name",
            "product",
            "product_name",
            "delivery_date",
            "delivered_at",
            "quantity",
            "unit_price",
            "line_total",
            "notes",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
