
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404

from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.suppliers.models import Supplier, SupplierMilkRate
from apps.suppliers.services import create_supplier_milk_rate
from apps.suppliers.api_serializers import (
    SupplierMilkRateCreateSerializer,
    SupplierMilkRateReadSerializer,
)


def raise_api_validation_error(exc):
    detail = getattr(exc, "message_dict", None)
    if detail is None:
        detail = exc.messages
    raise ValidationError(detail)


class SupplierMilkRatePermissionMixin:
    permission_classes = [IsAuthenticated, IsBusinessMember]

    def get_permissions(self):
        if self.request.method not in ("GET", "HEAD", "OPTIONS"):
            self.required_business_roles = [
                BusinessMembership.Role.OWNER,
                BusinessMembership.Role.MANAGER,
            ]
        else:
            self.required_business_roles = None

        return super().get_permissions()

    def get_supplier(self):
        return get_object_or_404(
            Supplier.objects.filter(
                business_id=self.request.business.id,
                is_active=True,
                is_deleted=False,
            ),
            pk=self.kwargs["supplier_id"],
        )


class SupplierMilkRateListCreateAPIView(
    SupplierMilkRatePermissionMixin,
    generics.ListCreateAPIView,
):
    def get_queryset(self):
        supplier = self.get_supplier()

        return SupplierMilkRate.objects.filter(
            business_id=self.request.business.id,
            supplier_id=supplier.id,
        ).order_by("-effective_from", "-created_at")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return SupplierMilkRateCreateSerializer

        return SupplierMilkRateReadSerializer

    def create(self, request, *args, **kwargs):
        input_serializer = self.get_serializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        supplier = self.get_supplier()
        data = input_serializer.validated_data

        try:
            rate = create_supplier_milk_rate(
                business_id=request.business.id,
                supplier_id=supplier.id,
                milk_type=data["milk_type"],
                rate=data["rate"],
                effective_from=data["effective_from"],
                effective_to=data.get("effective_to"),
                user=request.user,
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)
        except IntegrityError:
            # Includes database-level overlapping-rate protection.
            raise ValidationError({
                "effective_from": (
                    "The rate conflicts with an existing rate period. "
                    "Check the supplier's existing rate schedule."
                )
            })

        output = SupplierMilkRateReadSerializer(rate)
        return Response(
            output.data,
            status=status.HTTP_201_CREATED,
        )
