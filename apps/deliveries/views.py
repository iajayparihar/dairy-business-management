
from django.core.exceptions import (
    ObjectDoesNotExist,
    PermissionDenied as DjangoPermissionDenied,
    ValidationError as DjangoValidationError,
)

from rest_framework import status
from rest_framework.exceptions import (
    NotFound,
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
    DeliveryCancelSerializer,
    DeliveryCorrectSerializer,
    DeliveryCreateSerializer,
    DeliveryDetailSerializer,
    DeliveryFilterSerializer,
    DeliveryReadSerializer,
)
from apps.deliveries.services import (
    MANAGE_ROLES,
    cancel_delivery,
    correct_delivery,
    create_delivery,
)


class DeliveryViewMixin:
    permission_classes = [IsAuthenticated]

    def get_membership(self, request, business_id, roles=None):
        try:
            return require_business_membership(
                user=request.user,
                business_id=business_id,
                allowed_roles=roles,
            )
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(str(exc)) from exc
        except DjangoValidationError as exc:
            raise DRFValidationError(
                getattr(exc, "message_dict", None) or exc.messages
            ) from exc

    def run(self, service, **kwargs):
        """Call a service, mapping its errors to API responses."""
        try:
            return service(**kwargs)
        except ObjectDoesNotExist as exc:
            raise NotFound("Not found.") from exc
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(str(exc)) from exc
        except DjangoValidationError as exc:
            raise DRFValidationError(
                getattr(exc, "message_dict", None) or exc.messages
            ) from exc


class DeliveryListCreateView(DeliveryViewMixin, APIView):
    """List and create deliveries within a business."""

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


class DeliveryDetailView(DeliveryViewMixin, APIView):
    """Inspect a delivery (any member) or correct it (owner/manager)."""

    def get(self, request, business_id, pk):
        membership = self.get_membership(request, business_id)

        try:
            delivery = Delivery.objects.select_related(
                "customer", "product",
            ).get(pk=pk, business=membership.business)
        except Delivery.DoesNotExist as exc:
            raise NotFound("Delivery not found.") from exc

        return Response(DeliveryDetailSerializer(delivery).data)

    def patch(self, request, business_id, pk):
        self.get_membership(request, business_id, roles=MANAGE_ROLES)

        serializer = DeliveryCorrectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        replacement = self.run(
            correct_delivery,
            business_id=business_id,
            delivery_id=pk,
            user=request.user,
            **serializer.validated_data,
        )

        return Response(DeliveryDetailSerializer(replacement).data)


class DeliveryCancelView(DeliveryViewMixin, APIView):
    """Cancel a delivery with a reason (owner/manager)."""

    def post(self, request, business_id, pk):
        self.get_membership(request, business_id, roles=MANAGE_ROLES)

        serializer = DeliveryCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        delivery = self.run(
            cancel_delivery,
            business_id=business_id,
            delivery_id=pk,
            user=request.user,
            reason=serializer.validated_data["reason"],
        )

        return Response(DeliveryDetailSerializer(delivery).data)
