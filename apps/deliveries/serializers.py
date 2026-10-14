
from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.deliveries.models import Delivery


class DeliveryCreateSerializer(serializers.Serializer):
    """Validate input required to create a delivery."""

    customer_id = serializers.UUIDField()
    product_id = serializers.UUIDField()

    # Omit to use the server's current time.
    delivered_at = serializers.DateTimeField(required=False)

    shift = serializers.ChoiceField(
        choices=Delivery.Shift.choices,
        default=Delivery.Shift.MORNING,
    )

    # Only accepted for open-price ("other") products.
    unit_price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.00"),
        required=False,
    )

    # Client-generated key (e.g. a UUID) that makes retries safe.
    client_ref = serializers.CharField(
        max_length=64,
        required=False,
    )

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
            "shift",
            "status",
            "quantity",
            "unit_price",
            "line_total",
            "stock_shortfall",
            "client_ref",
            "replaces",
            "cancelled_at",
            "cancelled_by",
            "cancel_reason",
            "notes",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class DeliveryFilterSerializer(serializers.Serializer):
    """Validate list query parameters."""

    date = serializers.DateField(required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    customer = serializers.UUIDField(required=False)
    product = serializers.UUIDField(required=False)
    shift = serializers.ChoiceField(
        choices=Delivery.Shift.choices, required=False,
    )
    status = serializers.ChoiceField(
        choices=Delivery.Status.choices, required=False,
    )
    has_shortfall = serializers.BooleanField(required=False)
