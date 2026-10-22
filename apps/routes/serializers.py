from decimal import Decimal

from rest_framework import serializers

from apps.common.serializers import RejectUnknownFieldsMixin

from apps.routes.models import Route, RouteRun, RouteVisit, StandingOrder

SHIFTS = [("MORNING", "Morning"), ("EVENING", "Evening")]


class RouteSerializer(serializers.ModelSerializer):
    customer_count = serializers.IntegerField(source="stops.count", read_only=True)

    class Meta:
        model = Route
        fields = ["id", "name", "assigned_to", "is_active", "customer_count"]
        read_only_fields = fields


class RouteCreateSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    name = serializers.CharField(max_length=100)
    assigned_to = serializers.UUIDField(required=False, allow_null=True)


class RouteCustomersSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    customers = serializers.ListField(child=serializers.UUIDField(), max_length=2000)

    def validate_customers(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("A customer can appear only once on a route.")
        return value


class StandingOrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = StandingOrder
        fields = ["id", "customer", "customer_name", "product", "product_name", "shift", "quantity", "is_active"]
        read_only_fields = fields


class StandingOrderInputSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    customer_id = serializers.UUIDField()
    product_id = serializers.UUIDField()
    shift = serializers.ChoiceField(choices=SHIFTS, default="MORNING")
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))


class RunStartSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    route_id = serializers.UUIDField()
    run_date = serializers.DateField(required=False)
    shift = serializers.ChoiceField(choices=SHIFTS, default="MORNING")


class RunSerializer(serializers.ModelSerializer):
    route_name = serializers.CharField(source="route.name", read_only=True)

    class Meta:
        model = RouteRun
        fields = [
            "id", "route", "route_name", "run_date", "shift", "status", "assigned_to",
            "closed_at", "closed_by", "reopened_at", "reopened_by", "reopen_reason",
        ]
        read_only_fields = fields


class VisitSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_phone = serializers.CharField(source="customer.phone", read_only=True)
    customer_address = serializers.CharField(source="customer.address", read_only=True)

    class Meta:
        model = RouteVisit
        fields = [
            "id", "customer", "customer_name", "customer_phone", "customer_address",
            "sequence", "status", "reason", "recorded_at",
        ]
        read_only_fields = fields


class EntryItemSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    product_id = serializers.UUIDField()
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.00"), required=False)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class EntrySerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    customer_id = serializers.UUIDField()
    outcome = serializers.ChoiceField(choices=RouteVisit.Status.choices)
    items = EntryItemSerializer(many=True, required=False, max_length=50)
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    client_ref = serializers.CharField(max_length=40, required=False)


class ReopenSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    reason = serializers.CharField(max_length=500)
