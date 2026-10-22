
from django.db import IntegrityError
from rest_framework import serializers

from apps.billing.guards import customer_has_records
from apps.common.serializers import (
    RejectUnknownFieldsMixin,
    clean_phone,
    validate_party_code,
)

from apps.customers.models import Customer


def get_request_business(context):
    request = context.get("request")
    business = getattr(request, "business", None) if request else None

    if business is None:
        raise serializers.ValidationError(
            "An authorized business context is required."
        )

    return business


class CustomerSerializer(RejectUnknownFieldsMixin, serializers.ModelSerializer):
    business = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Customer
        fields = [
            "id",
            "business",
            "customer_code",
            "name",
            "phone",
            "email",
            "address",
            "customer_type",
            "notes",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "business",
            "created_at",
            "updated_at",
        ]

    def validate_phone(self, value):
        return clean_phone(value)

    def validate_customer_code(self, value):
        return validate_party_code(
            model=Customer,
            business=self.instance.business if self.instance else get_request_business(self.context),
            field="customer_code",
            value=value,
            instance=self.instance,
            has_records=customer_has_records,
        )

    def create(self, validated_data):
        business = get_request_business(self.context)
        try:
            return Customer.objects.create(business=business, **validated_data)
        except IntegrityError:  # lost a race with a parallel create of the same code
            raise serializers.ValidationError(
                {"customer_code": "This customer code is already used."}
            )

    def update(self, instance, validated_data):
        try:
            return super().update(instance, validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {"customer_code": "This customer code is already used."}
            )
