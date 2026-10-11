
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated

from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.common.querysets import scope_to_business
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

    def get_queryset(self):
        return scope_to_business(
            Supplier.objects.all(),
            self.request.business,
        )


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
