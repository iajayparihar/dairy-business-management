
import re

from django.db.models import Q
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated

from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.common.pagination import StandardPagination
from apps.common.querysets import scope_to_business
from apps.suppliers.guards import supplier_has_records
from apps.suppliers.models import Supplier
from apps.suppliers.serializers import SupplierSerializer


class BusinessScopedSupplierAPIViewMixin:
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


class SupplierListCreateAPIView(
    BusinessScopedSupplierAPIViewMixin,
    generics.ListCreateAPIView,
):
    serializer_class = SupplierSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        params = self.request.query_params
        # unique code as tie-break: pages must not skip or repeat equal names
        suppliers = scope_to_business(
            Supplier.objects.all(), self.request.business
        ).order_by("name", "supplier_code")
        active = params.get("is_active", "").lower()
        if active in ("true", "false"):
            suppliers = suppliers.filter(is_active=active == "true")
        elif active:
            raise ValidationError({"is_active": "Use true or false."})
        kind = params.get("supplier_type", "")
        if kind:
            if kind not in Supplier.SupplierType.values:
                raise ValidationError({"supplier_type": "Unknown supplier type."})
            suppliers = suppliers.filter(supplier_type=kind)
        term = params.get("search", "").strip()
        if term:
            digits = re.sub(r"[\s\-().]", "", term)
            suppliers = suppliers.filter(
                Q(name__icontains=term) | Q(supplier_code__icontains=term)
                | Q(phone__icontains=digits)
            )
        return suppliers


class SupplierDetailAPIView(
    BusinessScopedSupplierAPIViewMixin,
    generics.RetrieveUpdateDestroyAPIView,
):
    serializer_class = SupplierSerializer

    def get_queryset(self):
        return scope_to_business(
            Supplier.objects.all(),
            self.request.business,
        )

    def perform_destroy(self, instance):
        if supplier_has_records(instance):
            raise ValidationError({
                "detail": (
                    "This supplier has milk, purchases or payments and cannot be "
                    "deleted. Mark the supplier inactive instead."
                )
            })
        instance.soft_delete(self.request.user)
