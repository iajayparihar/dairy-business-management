from decimal import Decimal

from rest_framework import serializers

from apps.billing.models import CustomerBill, CustomerPayment
from apps.billing.documents import money
from apps.purchases.api_serializers import TimezoneAwareDateTimeField


class StrictSerializer(serializers.Serializer):
    """Unknown fields are an error, so a typo never silently changes meaning."""

    def to_internal_value(self, data):
        unknown = set(getattr(data, "keys", lambda: [])()) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({
                name: "This field is not accepted." for name in sorted(unknown)
            })
        return super().to_internal_value(data)


def _amount(**kwargs):
    return serializers.DecimalField(max_digits=14, decimal_places=2, **kwargs)


class AllocationItemSerializer(StrictSerializer):
    bill_id = serializers.UUIDField()
    amount = _amount(min_value=Decimal("0.01"))


class PaymentCreateSerializer(StrictSerializer):
    amount = _amount(min_value=Decimal("0.01"))
    payment_method = serializers.ChoiceField(
        choices=CustomerPayment.PaymentMethod.choices,
        default=CustomerPayment.PaymentMethod.CASH,
    )
    paid_at = TimezoneAwareDateTimeField(required=False)
    reference_number = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default="",
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    client_ref = serializers.CharField(
        max_length=64, required=False, allow_blank=True,
    )
    allocations = AllocationItemSerializer(many=True, required=False)


class PaymentReverseSerializer(StrictSerializer):
    amount = _amount(min_value=Decimal("0.01"), required=False)
    reason = serializers.CharField(trim_whitespace=True)
    client_ref = serializers.CharField(
        max_length=64, required=False, allow_blank=True,
    )


class PaymentAllocateSerializer(StrictSerializer):
    bill_id = serializers.UUIDField()
    amount = _amount(min_value=Decimal("0.01"))


class PeriodSerializer(StrictSerializer):
    period_start = serializers.DateField()
    period_end = serializers.DateField()


class BulkGenerateSerializer(PeriodSerializer):
    customer_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, allow_empty=False,
    )


class FinalizeSerializer(StrictSerializer):
    expected_total = _amount(min_value=Decimal("0.00"), required=False)


class VoidSerializer(StrictSerializer):
    reason = serializers.CharField(trim_whitespace=True)
    create_replacement = serializers.BooleanField(required=False)


class OpeningBalanceSerializer(StrictSerializer):
    amount = _amount()
    as_of_date = serializers.DateField()
    client_ref = serializers.CharField(
        max_length=64, required=False, allow_blank=True,
    )


def serialize_payment(payment):
    return {
        "id": str(payment.pk),
        "customer_id": str(payment.customer_id),
        "payment_number": payment.payment_number,
        "paid_at": payment.paid_at,
        "amount": money(payment.amount),
        "payment_method": payment.payment_method,
        "reference_number": payment.reference_number,
        "notes": payment.notes,
        "client_ref": payment.client_ref,
        "created_by": str(payment.created_by_id) if payment.created_by_id else None,
        "created_at": payment.created_at,
    }


def serialize_allocation(allocation):
    unwound = sum((u.amount for u in allocation.unwinds.all()), Decimal("0.00"))
    return {
        "id": str(allocation.pk),
        "bill_id": str(allocation.bill_id),
        "bill_number": allocation.bill.bill_number,
        "bill_status": allocation.bill.status,
        "amount": money(allocation.amount),
        "unwound": money(unwound),
        "effective": money(allocation.amount - unwound),
        "created_at": allocation.created_at,
    }


def serialize_reversal(reversal, unwinds):
    unwound_total = sum((u.amount for u in unwinds), Decimal("0.00"))
    return {
        "id": str(reversal.pk),
        "payment_id": str(reversal.payment_id),
        "amount": money(reversal.amount),
        "from_advance": money(reversal.amount - unwound_total),
        "from_allocations": [
            {
                "allocation_id": str(u.allocation_id),
                "bill_id": str(u.allocation.bill_id),
                "bill_number": u.allocation.bill.bill_number,
                "amount": money(u.amount),
            }
            for u in unwinds
        ],
        "reason": reversal.reason,
        "reversed_at": reversal.reversed_at,
        "client_ref": reversal.client_ref,
        "created_by": str(reversal.created_by_id) if reversal.created_by_id else None,
        "created_at": reversal.created_at,
    }


def jsonable_money(value):
    """Decimals in plain dicts (position, statement) as 2-decimal strings."""
    if isinstance(value, Decimal):
        return money(value)
    if isinstance(value, dict):
        return {k: jsonable_money(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable_money(v) for v in value]
    return value
