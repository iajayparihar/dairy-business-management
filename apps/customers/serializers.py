
from rest_framework import serializers

from apps.customers.models import Customer


def get_request_business(context):
    request = context.get("request")
    business = getattr(request, "business", None) if request else None

    if business is None:
        raise serializers.ValidationError(
            "An authorized business context is required."
        )

    return business


class CustomerSerializer(serializers.ModelSerializer):
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

    def create(self, validated_data):
        business = get_request_business(self.context)
        return Customer.objects.create(
            business=business,
            **validated_data,
        )
