
from rest_framework import serializers

from apps.purchases.models import MilkCollection


class TimezoneAwareDateTimeField(serializers.DateTimeField):
    """
    Require the client to submit a timestamp containing a timezone,
    such as +05:30 or Z.
    """

    def to_internal_value(self, value):
        from django.utils.dateparse import parse_datetime

        if isinstance(value, str):
            parsed = parse_datetime(value)
            if parsed is None or parsed.utcoffset() is None:
                raise serializers.ValidationError(
                    "Provide an ISO-8601 timestamp with a timezone."
                )

        result = super().to_internal_value(value)

        if result.utcoffset() is None:
            raise serializers.ValidationError(
                "Provide a timezone-aware timestamp."
            )

        return result


class MilkCollectionCreateSerializer(serializers.Serializer):
    collection_number = serializers.CharField(
        max_length=50,
        trim_whitespace=True,
    )
    milk_type = serializers.ChoiceField(
        choices=MilkCollection.MilkType.choices,
    )
    collected_at = TimezoneAwareDateTimeField()
    litres = serializers.DecimalField(
        max_digits=12,
        decimal_places=3,
        min_value=0.001,
    )
    fat = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
    )


class MilkCollectionReadSerializer(serializers.ModelSerializer):
    business = serializers.UUIDField(
        source="business_id",
        read_only=True,
    )
    supplier = serializers.UUIDField(
        source="supplier_id",
        read_only=True,
    )
    rate_record = serializers.UUIDField(
        source="rate_record_id",
        read_only=True,
    )
    purchase = serializers.UUIDField(
        source="purchase_id",
        read_only=True,
        allow_null=True,
    )
    created_by = serializers.UUIDField(
        source="created_by_id",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = MilkCollection
        fields = [
            "id",
            "business",
            "supplier",
            "rate_record",
            "collection_number",
            "milk_type",
            "collected_at",
            "litres",
            "fat",
            "applied_rate",
            "amount",
            "purchase",
            "created_by",
            "created_at",
        ]
        read_only_fields = fields
