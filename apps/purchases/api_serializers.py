
from rest_framework import serializers
from decimal import Decimal
from apps.purchases.models import (
    MilkCollection,
    PurchaseAdjustment,
    PurchaseLine,
    SupplierPurchase,
)


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
        min_value=Decimal("0.001"),
    )
    fat = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0.00"),
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


class PurchaseLineInputSerializer(serializers.Serializer):
    product_id = serializers.UUIDField()
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0.001"),
    )
    unit_price = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=Decimal("0"),
    )


class PurchaseCreateSerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField()
    purchase_number = serializers.CharField(max_length=50, trim_whitespace=True)
    purchased_at = TimezoneAwareDateTimeField()
    notes = serializers.CharField(allow_blank=True, required=False, default="")
    lines = PurchaseLineInputSerializer(many=True, allow_empty=False)


class PurchaseUpdateSerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField(required=False)
    purchased_at = TimezoneAwareDateTimeField(required=False)
    notes = serializers.CharField(allow_blank=True, required=False)
    lines = PurchaseLineInputSerializer(many=True, allow_empty=False, required=False)


class PurchaseCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=True, required=False, default="")


class PurchaseFilterSerializer(serializers.Serializer):
    supplier = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(
        choices=SupplierPurchase.Status.choices, required=False,
    )
    purchase_number = serializers.CharField(required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs):
        if (
            "date_from" in attrs and "date_to" in attrs
            and attrs["date_from"] > attrs["date_to"]
        ):
            raise serializers.ValidationError(
                {"date_to": "Must be on or after date_from."}
            )
        return attrs


class PurchaseLineReadSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = PurchaseLine
        fields = [
            "id", "product_id", "product_name",
            "quantity", "unit_price", "line_total",
        ]
        read_only_fields = fields


class PurchaseReadSerializer(serializers.ModelSerializer):
    supplier_id = serializers.UUIDField(read_only=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    lines = PurchaseLineReadSerializer(many=True, read_only=True)

    class Meta:
        model = SupplierPurchase
        fields = [
            "id", "supplier_id", "supplier_name", "purchase_number",
            "purchased_at", "status", "notes", "total_amount", "lines",
            "received_at", "cancelled_at", "cancel_reason", "created_at",
        ]
        read_only_fields = fields


class _AdjustmentBase(serializers.Serializer):
    reason = serializers.CharField(trim_whitespace=True)
    client_ref = serializers.CharField(max_length=64, required=False, allow_null=True)


class ReturnLineInputSerializer(serializers.Serializer):
    purchase_line_id = serializers.UUIDField()
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0.001"),
    )


class CorrectionLineInputSerializer(serializers.Serializer):
    purchase_line_id = serializers.UUIDField()
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0.001"), required=False,
    )
    unit_price = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=Decimal("0"), required=False,
    )


class PurchaseReturnSerializer(_AdjustmentBase):
    lines = ReturnLineInputSerializer(many=True, allow_empty=False)


class PurchaseCorrectionSerializer(_AdjustmentBase):
    lines = CorrectionLineInputSerializer(many=True, allow_empty=False)


class PurchaseAdjustmentReadSerializer(serializers.ModelSerializer):
    lines = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseAdjustment
        fields = [
            "id", "adjustment_type", "reason", "amount", "occurred_at",
            "client_ref", "created_by_id", "created_at", "lines",
        ]
        read_only_fields = fields

    def get_lines(self, obj):
        return [
            {
                "purchase_line_id": str(l.purchase_line_id),
                "quantity_before": l.quantity_before,
                "quantity_after": l.quantity_after,
                "unit_price_before": l.unit_price_before,
                "unit_price_after": l.unit_price_after,
                "amount": l.amount,
                "stock_movement_id": (
                    str(l.stock_movement_id) if l.stock_movement_id else None
                ),
            }
            for l in obj.lines.all()
        ]
