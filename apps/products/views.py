
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.common.querysets import scope_to_business
from apps.common.pagination import StandardPagination
from apps.products.guards import category_has_products, product_has_records
from apps.products.models import Product, ProductCategory, ProductPrice
from apps.products.serializers import (
    ProductCategorySerializer,
    ProductPriceCreateSerializer,
    ProductPriceSerializer,
    ProductSerializer,
)
from apps.products.services import change_product_price
from apps.purchases.api_views import raise_api_validation_error


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


def _bool_param(params, name):
    raw = params.get(name, "").lower()
    if raw in ("true", "false"):
        return raw == "true"
    if raw:
        raise serializers.ValidationError({name: "Use true or false."})
    return None


class ProductCategoryListCreateAPIView(
    BusinessScopedAPIViewMixin,
    generics.ListCreateAPIView,
):
    serializer_class = ProductCategorySerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        rows = scope_to_business(
            ProductCategory.objects.all(), self.request.business
        ).order_by("name", "id")
        active = _bool_param(self.request.query_params, "is_active")
        return rows if active is None else rows.filter(is_active=active)


class ProductCategoryDetailAPIView(
    BusinessScopedAPIViewMixin,
    generics.RetrieveUpdateDestroyAPIView,
):
    serializer_class = ProductCategorySerializer

    def get_queryset(self):
        return scope_to_business(ProductCategory.objects.all(), self.request.business)

    def perform_destroy(self, instance):
        if category_has_products(instance):
            raise serializers.ValidationError({
                "detail": "This category has products. Move them or mark it inactive."
            })
        instance.soft_delete(self.request.user)


class ProductListCreateAPIView(
    BusinessScopedAPIViewMixin,
    generics.ListCreateAPIView,
):
    serializer_class = ProductSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        params = self.request.query_params
        rows = scope_to_business(
            Product.objects.select_related("category"), self.request.business
        ).order_by("name", "sku")
        for name, field in (("is_active", "is_active"), ("track_stock", "track_stock")):
            flag = _bool_param(params, name)
            if flag is not None:
                rows = rows.filter(**{field: flag})
        unit = params.get("unit", "")
        if unit:
            if unit not in Product.Unit.values:
                raise serializers.ValidationError({"unit": "Unknown unit."})
            rows = rows.filter(unit=unit)
        category = params.get("category", "")
        if category:
            try:
                rows = rows.filter(category_id=serializers.UUIDField().run_validation(category))
            except serializers.ValidationError:
                raise serializers.ValidationError({"category": "Not a valid id."})
        term = params.get("search", "").strip()
        if term:
            rows = rows.filter(Q(name__icontains=term) | Q(sku__icontains=term))
        return rows


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

    def perform_destroy(self, instance):
        if product_has_records(instance):
            raise serializers.ValidationError({
                "detail": (
                    "This product has sales, stock, purchases or deliveries and "
                    "cannot be deleted. Mark it inactive instead."
                )
            })
        instance.soft_delete(self.request.user)


class ProductPriceListCreateAPIView(BusinessScopedAPIViewMixin, APIView):
    """
    Price history of one product, and scheduling a new price.

    Selling prices are visible to every member; PURCHASE (cost) prices are
    cost data and visible to OWNER/MANAGER only. Only OWNER/MANAGER can
    change a price, never into the past (`change_product_price`).
    """

    def product(self, request, pk):
        return get_object_or_404(
            Product.objects, pk=pk, business=request.business
        )

    def get(self, request, business_id, pk):
        product = self.product(request, pk)
        rows = ProductPrice.objects.filter(
            business=request.business, product=product
        ).order_by("-effective_from")
        membership = request.business_membership
        if membership is not None and membership.role not in (
            BusinessMembership.Role.OWNER,
            BusinessMembership.Role.MANAGER,
        ):
            rows = rows.filter(price_type=ProductPrice.PriceType.SELLING)
        wanted = serializers.ChoiceField(
            choices=ProductPrice.PriceType.values
        )
        if "price_type" in request.query_params:
            rows = rows.filter(
                price_type=wanted.run_validation(request.query_params["price_type"])
            )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(rows, request)
        return paginator.get_paginated_response(
            ProductPriceSerializer(page, many=True).data
        )

    def post(self, request, business_id, pk):
        product = self.product(request, pk)
        body = ProductPriceCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            price = change_product_price(
                business_id=request.business.id,
                product_id=product.pk,
                user=request.user,
                price_type=body.validated_data["price_type"],
                unit_price=body.validated_data["unit_price"],
                effective_from=body.validated_data.get("effective_from"),
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)
        return Response(
            ProductPriceSerializer(price).data, status=status.HTTP_201_CREATED
        )
