
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated

from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.common.querysets import scope_to_business
from apps.products.models import Product, ProductCategory
from apps.products.serializers import (
    ProductCategorySerializer,
    ProductSerializer,
)


class BusinessScopedAPIViewMixin:
    """
    Require business membership and scope data to that business.

    All active members may read. Only owners and managers may write.
    """

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


class ProductCategoryListCreateAPIView(
    BusinessScopedAPIViewMixin,
    generics.ListCreateAPIView,
):
    serializer_class = ProductCategorySerializer

    def get_queryset(self):
        return scope_to_business(
            ProductCategory.objects.all(),
            self.request.business,
        )


class ProductListCreateAPIView(
    BusinessScopedAPIViewMixin,
    generics.ListCreateAPIView,
):
    serializer_class = ProductSerializer

    def get_queryset(self):
        return scope_to_business(
            Product.objects.select_related("category"),
            self.request.business,
        )


class ProductDetailAPIView(
    BusinessScopedAPIViewMixin,
    generics.RetrieveUpdateDestroyAPIView,
):
    serializer_class = ProductSerializer
    lookup_field = "pk"

    def get_queryset(self):
        return scope_to_business(
            Product.objects.select_related("category"),
            self.request.business,
        )
