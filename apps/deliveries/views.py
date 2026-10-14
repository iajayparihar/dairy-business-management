
from django.core.exceptions import (
    PermissionDenied as DjangoPermissionDenied,
    ValidationError as DjangoValidationError,
)

from rest_framework import status
from rest_framework.exceptions import (
    PermissionDenied,
    ValidationError as DRFValidationError,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.businesses.services import require_business_membership
from apps.deliveries.models import Delivery
from apps.common.pagination import StandardPagination
from apps.deliveries.serializers import (
    DeliveryCreateSerializer,
    DeliveryFilterSerializer,
    DeliveryReadSerializer,
)
from apps.deliveries.services import create_delivery


class DeliveryListCreateView(APIView):
    """List and create deliveries within a business."""

    permission_classes = [IsAuthenticated]

    def get_membership(self, request, business_id):
        try:
            return require_business_membership(
                user=request.user,
                business_id=business_id,
            )
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(str(exc)) from exc
        except DjangoValidationError as exc:
            raise DRFValidationError(
                getattr(exc, "message_dict", None) or exc.messages
            ) from exc

    def get(self, request, business_id):
        membership = self.get_membership(request, business_id)

        filters = DeliveryFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        f = filters.validated_data

        deliveries = (
            Delivery.objects.filter(
                business=membership.business,
            )
            .select_related("customer", "product")
            .order_by("-delivered_at", "-created_at")
        )

        for param, lookup in (
            ("date", "delivery_date"),
            ("date_from", "delivery_date__gte"),
            ("date_to", "delivery_date__lte"),
            ("customer", "customer_id"),
            ("product", "product_id"),
            ("shift", "shift"),
            ("status", "status"),
        ):
            if param in f:
                deliveries = deliveries.filter(**{lookup: f[param]})

        if f.get("has_shortfall"):
            deliveries = deliveries.filter(stock_shortfall__gt=0)

        paginator = StandardPagination()
        page = paginator.paginate_queryset(deliveries, request)

        return paginator.get_paginated_response(
            DeliveryReadSerializer(page, many=True).data
        )

    def post(self, request, business_id):
        membership = self.get_membership(request, business_id)

        input_serializer = DeliveryCreateSerializer(
            data=request.data,
        )
        input_serializer.is_valid(raise_exception=True)

        try:
            delivery = create_delivery(
                business_id=membership.business_id,
                user=request.user,
                **input_serializer.validated_data,
            )
        except DjangoValidationError as exc:
            raise DRFValidationError(
                getattr(exc, "message_dict", None) or exc.messages
            ) from exc

        output_serializer = DeliveryReadSerializer(delivery)

        return Response(
            output_serializer.data,
            status=status.HTTP_201_CREATED,
        )
