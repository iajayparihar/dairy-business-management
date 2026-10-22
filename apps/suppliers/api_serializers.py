
from rest_framework import serializers
from decimal import Decimal
from apps.common.serializers import RejectUnknownFieldsMixin, StrictDateTimeField
from apps.suppliers.models import SupplierMilkRate


class SupplierMilkRateReadSerializer(serializers.ModelSerializer):
    business = serializers.UUIDField(
        source="business_id",
        read_only=True,
    )
    supplier = serializers.UUIDField(
        source="supplier_id",
        read_only=True,
    )
    created_by = serializers.UUIDField(
        source="created_by_id",
        read_only=True,
        allow_null=True,
    )
    cancelled_by = serializers.UUIDField(
        source="cancelled_by_id", read_only=True, allow_null=True,
    )

    class Meta:
        model = SupplierMilkRate
        fields = [
            "id",
            "business",
            "supplier",
            "milk_type",
            "rate",
            "effective_from",
            "effective_to",
            "created_by",
            "created_at",
            "cancelled_at",
            "cancelled_by",
            "cancel_reason",
        ]
        read_only_fields = fields


class SupplierMilkRateCreateSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    milk_type = serializers.ChoiceField(
        choices=SupplierMilkRate.MilkType.choices,
    )
    rate = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.001"),
    )
    effective_from = StrictDateTimeField()
    effective_to = StrictDateTimeField(
        required=False,
        allow_null=True,
        default=None,
    )

    def validate(self, attrs):
        effective_from = attrs["effective_from"]
        effective_to = attrs.get("effective_to")

        if effective_from.utcoffset() is None:
            raise serializers.ValidationError({
                "effective_from": "Provide a timezone-aware timestamp."
            })

        if effective_to is not None:
            if effective_to.utcoffset() is None:
                raise serializers.ValidationError({
                    "effective_to": "Provide a timezone-aware timestamp."
                })

            if effective_to <= effective_from:
                raise serializers.ValidationError({
                    "effective_to": (
                        "Must be later than effective_from."
                    )
                })

        return attrs


class SupplierMilkRateCorrectSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    milk_type = serializers.ChoiceField(choices=SupplierMilkRate.MilkType.choices)
    effective_from = StrictDateTimeField()
    effective_to = StrictDateTimeField()
    rate = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    reason = serializers.CharField(max_length=500, trim_whitespace=True)
    confirm = serializers.BooleanField(default=False)
    # confirm=true must repeat what the preview showed
    expected_count = serializers.IntegerField(required=False, min_value=0)
    expected_new_total = serializers.DecimalField(
        required=False, max_digits=16, decimal_places=2,
    )

    def validate(self, attrs):
        if attrs["confirm"] and ("expected_count" not in attrs or "expected_new_total" not in attrs):
            raise serializers.ValidationError(
                "Send expected_count and expected_new_total from the preview to confirm."
            )
        return attrs
