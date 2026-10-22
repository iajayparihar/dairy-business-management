
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404

from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.common.pagination import StandardPagination
from apps.suppliers.models import Supplier, SupplierMilkRate
from apps.common.serializers import decimals_to_str
from apps.suppliers.rate_correction import correct_supplier_milk_rate
from apps.suppliers.services import create_supplier_milk_rate
from apps.suppliers.api_serializers import (
    SupplierMilkRateCorrectSerializer,
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
        # Supplier rates are cost data: OWNER/MANAGER only, reads included.
        self.required_business_roles = [
            BusinessMembership.Role.OWNER,
            BusinessMembership.Role.MANAGER,
        ]
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
    pagination_class = StandardPagination

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


class SupplierMilkRateCorrectAPIView(generics.GenericAPIView):
    """
    OWNER only. POST without `confirm` (or confirm=false) is a preview that writes nothing;
    confirm=true must repeat the preview's expected_count and expected_new_total.
    """

    permission_classes = [IsAuthenticated, IsBusinessMember]
    serializer_class = SupplierMilkRateCorrectSerializer

    def get_permissions(self):
        self.required_business_roles = [BusinessMembership.Role.OWNER]
        return super().get_permissions()

    def post(self, request, business_id, supplier_id):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = correct_supplier_milk_rate(
                business_id=request.business.id, supplier_id=supplier_id, user=request.user,
                milk_type=data["milk_type"], effective_from=data["effective_from"],
                effective_to=data["effective_to"], rate=data["rate"], reason=data["reason"],
                confirm=data["confirm"], expected_count=data.get("expected_count"),
                expected_new_total=data.get("expected_new_total"),
            )
        except Supplier.DoesNotExist:
            raise NotFound("Supplier not found.")
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)
        return Response(decimals_to_str(result), status=status.HTTP_200_OK)
