
from django.db import IntegrityError
from rest_framework import serializers

from apps.common.serializers import (
    RejectUnknownFieldsMixin,
    clean_phone,
    validate_party_code,
)
from apps.suppliers.guards import supplier_has_records

from apps.suppliers.models import Supplier


class SupplierSerializer(RejectUnknownFieldsMixin, serializers.ModelSerializer):
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

    def validate_phone(self, value):
        return clean_phone(value)

    def _business(self):
        if self.instance:
            return self.instance.business
        request = self.context.get("request")
        business = getattr(request, "business", None) if request else None
        if business is None:
            raise serializers.ValidationError("An authorized business context is required.")
        return business

    def validate_supplier_code(self, value):
        return validate_party_code(
            model=Supplier,
            business=self._business(),
            field="supplier_code",
            value=value,
            instance=self.instance,
            has_records=supplier_has_records,
        )

    def create(self, validated_data):
        try:
            return Supplier.objects.create(business=self._business(), **validated_data)
        except IntegrityError:  # lost a race with a parallel create of the same code
            raise serializers.ValidationError(
                {"supplier_code": "This code is already used."}
            )

    def update(self, instance, validated_data):
        try:
            return super().update(instance, validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {"supplier_code": "This code is already used."}
            )
