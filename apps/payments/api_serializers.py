
from rest_framework import serializers
from decimal import Decimal
from apps.payments.models import SupplierPayment


class SupplierPaymentCreateSerializer(serializers.Serializer):
    payment_number = serializers.CharField(max_length=50, trim_whitespace=True)
    paid_at = serializers.DateTimeField()
    amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.001"),
    )
    payment_method = serializers.ChoiceField(
        choices=SupplierPayment.PaymentMethod.choices,
        default=SupplierPayment.PaymentMethod.CASH,
    )
    reference_number = serializers.CharField(
        max_length=100,
        required=False,
        allow_blank=True,
        default="",
    )
    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
    )

    def validate_paid_at(self, value):
        if value.utcoffset() is None:
            raise serializers.ValidationError(
                "Provide an ISO-8601 timestamp with a timezone."
            )
        return value

    def validate_amount(self, value):
        if not value.is_finite() or value <= 0:
            raise serializers.ValidationError(
                "Payment amount must be positive and finite."
            )
        return value


class SupplierPaymentReadSerializer(serializers.ModelSerializer):
    business = serializers.UUIDField(source="business_id", read_only=True)
    supplier = serializers.UUIDField(source="supplier_id", read_only=True)
    created_by = serializers.UUIDField(
        source="created_by_id",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = SupplierPayment
        fields = [
            "id",
            "business",
            "supplier",
            "payment_number",
            "paid_at",
            "amount",
            "payment_method",
            "reference_number",
            "notes",
            "created_by",
            "created_at",
        ]
        read_only_fields = fields
