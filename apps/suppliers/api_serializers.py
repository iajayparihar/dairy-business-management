
from rest_framework import serializers

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
        ]
        read_only_fields = fields


class SupplierMilkRateCreateSerializer(serializers.Serializer):
    milk_type = serializers.ChoiceField(
        choices=SupplierMilkRate.MilkType.choices,
    )
    rate = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=0,
    )
    effective_from = serializers.DateTimeField()
    effective_to = serializers.DateTimeField(
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
