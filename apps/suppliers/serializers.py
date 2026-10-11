
from rest_framework import serializers

from apps.suppliers.models import Supplier


class SupplierSerializer(serializers.ModelSerializer):
    business = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Supplier
        fields = [
            "id",
            "business",
            "supplier_code",
            "name",
            "phone",
            "email",
            "address",
            "supplier_type",
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
        request = self.context.get("request")
        business = getattr(request, "business", None) if request else None

        if business is None:
            raise serializers.ValidationError(
                "An authorized business context is required."
            )

        return Supplier.objects.create(
            business=business,
            **validated_data,
        )
