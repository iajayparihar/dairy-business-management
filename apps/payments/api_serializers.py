
from rest_framework import serializers
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta

from apps.common.serializers import RejectUnknownFieldsMixin, StrictDateTimeField
from apps.payments.models import SupplierPayment


class SupplierPaymentCreateSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    payment_number = serializers.CharField(max_length=50, trim_whitespace=True)
    paid_at = StrictDateTimeField()
    amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
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
    client_ref = serializers.CharField(
        max_length=64,
        required=False,
        allow_blank=True,
    )

    def validate_paid_at(self, value):
        # never in the future (5 minutes of phone-clock skew allowed)
        if value > timezone.now() + timedelta(minutes=5):
            raise serializers.ValidationError("The payment date cannot be in the future.")
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
            "client_ref",
            "created_by",
            "created_at",
        ]
        read_only_fields = fields


class PaymentReverseSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    # Omit amount to reverse everything that is left of the payment.
    amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
        required=False,
    )
    reason = serializers.CharField(trim_whitespace=True)
    client_ref = serializers.CharField(
        max_length=64,
        required=False,
        allow_blank=True,
    )


class PaymentAllocateSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    purchase_id = serializers.UUIDField()
    amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )


def serialize_reversal(reversal, unwinds):
    """unwinds: that reversal's allocation-reversal rows (with purchase)."""
    unwound_total = sum((u.amount for u in unwinds), Decimal("0.00"))
    return {
        "id": str(reversal.id),
        "payment_id": str(reversal.payment_id),
        "amount": reversal.amount,
        "from_advance": reversal.amount - unwound_total,
        "from_allocations": [
            {
                "allocation_id": str(u.allocation_id),
                "purchase_id": str(u.allocation.purchase_id),
                "purchase_number": u.allocation.purchase.purchase_number,
                "amount": u.amount,
            }
            for u in unwinds
        ],
        "reason": reversal.reason,
        "reversed_at": reversal.reversed_at,
        "client_ref": reversal.client_ref,
        "created_by": (
            str(reversal.created_by_id) if reversal.created_by_id else None
        ),
        "created_at": reversal.created_at,
    }


def serialize_allocation(allocation):
    unwound = sum((u.amount for u in allocation.unwinds.all()), Decimal("0.00"))
    return {
        "id": str(allocation.id),
        "purchase_id": str(allocation.purchase_id),
        "purchase_number": allocation.purchase.purchase_number,
        "purchase_status": allocation.purchase.status,
        "amount": allocation.amount,
        "unwound": unwound,
        "effective": allocation.amount - unwound,
        "created_at": allocation.created_at,
    }
