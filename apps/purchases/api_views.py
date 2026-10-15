
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date

from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.purchases.api_serializers import (
    MilkCollectionCreateSerializer,
    MilkCollectionReadSerializer,
)
from apps.purchases.models import MilkCollection
from apps.purchases.services import create_milk_collection
from apps.suppliers.models import Supplier


def raise_api_validation_error(exc):
    detail = getattr(exc, "message_dict", None)
    if detail is None:
        detail = exc.messages
    raise ValidationError(detail)


class MilkCollectionPermissionMixin:
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


class MilkCollectionListCreateAPIView(
    MilkCollectionPermissionMixin,
    generics.ListCreateAPIView,
):
    def get_queryset(self):
        supplier = self.get_supplier()

        queryset = MilkCollection.objects.filter(
            business_id=self.request.business.id,
            supplier_id=supplier.id,
        ).select_related("rate_record", "purchase")

        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")

        if start_date:
            parsed_start = parse_date(start_date)
            if parsed_start is None:
                raise ValidationError({
                    "start_date": "Use YYYY-MM-DD format."
                })
            queryset = queryset.filter(
                collected_at__date__gte=parsed_start,
            )

        if end_date:
            parsed_end = parse_date(end_date)
            if parsed_end is None:
                raise ValidationError({
                    "end_date": "Use YYYY-MM-DD format."
                })
            queryset = queryset.filter(
                collected_at__date__lte=parsed_end,
            )

        if start_date and end_date and parsed_start > parsed_end:
            raise ValidationError({
                "end_date": "Must be on or after start_date."
            })

        return queryset.order_by("-collected_at", "-created_at")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return MilkCollectionCreateSerializer
        return MilkCollectionReadSerializer

    def create(self, request, *args, **kwargs):
        input_serializer = self.get_serializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        supplier = self.get_supplier()
        data = input_serializer.validated_data

        try:
            collection = create_milk_collection(
                business_id=request.business.id,
                supplier_id=supplier.id,
                collection_number=data["collection_number"],
                milk_type=data["milk_type"],
                collected_at=data["collected_at"],
                litres=data["litres"],
                fat=data["fat"],
                user=request.user,
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)
        except IntegrityError:
            # Database constraints remain the final duplicate guard.
            raise ValidationError({
                "collection_number": (
                    "This collection or its linked purchase number "
                    "already exists."
                )
            })

        output = MilkCollectionReadSerializer(collection)
        return Response(
            output.data,
            status=status.HTTP_201_CREATED,
        )


class MilkCollectionDetailAPIView(
    MilkCollectionPermissionMixin,
    generics.RetrieveAPIView,
):
    serializer_class = MilkCollectionReadSerializer
    lookup_url_kwarg = "collection_id"

    def get_queryset(self):
        supplier = self.get_supplier()

        return MilkCollection.objects.filter(
            business_id=self.request.business.id,
            supplier_id=supplier.id,
        ).select_related("rate_record", "purchase")


# ---- General supplier purchases (draft -> received / cancelled) ----

from django.core.exceptions import (
    ObjectDoesNotExist,
    PermissionDenied as DjangoPermissionDenied,
)
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.views import APIView

from apps.businesses.services import require_business_membership
from apps.common.pagination import StandardPagination
from apps.purchases.adjustments import (
    correct_purchase_entry,
    return_purchase_goods,
)
from apps.purchases.api_serializers import (
    PurchaseAdjustmentReadSerializer,
    PurchaseCancelSerializer,
    PurchaseCorrectionSerializer,
    PurchaseReturnSerializer,
    PurchaseCreateSerializer,
    PurchaseFilterSerializer,
    PurchaseReadSerializer,
    PurchaseUpdateSerializer,
)
from apps.purchases.models import SupplierPurchase
from apps.purchases.services import (
    ALLOWED_ROLES,
    cancel_draft_purchase,
    create_purchase,
    receive_purchase,
    update_draft_purchase,
)


def _lines(data):
    return [
        {**line, "product_id": str(line["product_id"])}
        for line in data
    ]


class PurchaseViewMixin:
    """Purchases carry cost data: OWNER/MANAGER only, business-scoped."""

    permission_classes = [IsAuthenticated]

    def membership(self, request, business_id):
        try:
            return require_business_membership(
                user=request.user,
                business_id=business_id,
                allowed_roles=ALLOWED_ROLES,
            )
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(str(exc)) from exc

    def queryset(self, business_id):
        return SupplierPurchase.objects.filter(
            business_id=business_id
        ).select_related("supplier").prefetch_related("lines__product")

    def run(self, service, **kwargs):
        try:
            return service(**kwargs)
        except ObjectDoesNotExist as exc:
            # Same answer for missing and foreign-business IDs.
            raise NotFound("Not found.") from exc
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(str(exc)) from exc
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)
        except IntegrityError:
            raise ValidationError({
                "purchase_number": "This purchase number already exists."
            })

    def respond(self, business_id, purchase_id, code=status.HTTP_200_OK):
        purchase = self.queryset(business_id).get(pk=purchase_id)
        return Response(PurchaseReadSerializer(purchase).data, status=code)


class PurchaseListCreateAPIView(PurchaseViewMixin, APIView):
    def get(self, request, business_id):
        self.membership(request, business_id)

        filters = PurchaseFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        f = filters.validated_data

        purchases = self.queryset(business_id)
        for param, lookup in (
            ("supplier", "supplier_id"),
            ("status", "status"),
            ("purchase_number", "purchase_number__icontains"),
            ("date_from", "purchased_at__date__gte"),
            ("date_to", "purchased_at__date__lte"),
        ):
            if param in f:
                purchases = purchases.filter(**{lookup: f[param]})

        paginator = StandardPagination()
        page = paginator.paginate_queryset(purchases, request)
        return paginator.get_paginated_response(
            PurchaseReadSerializer(page, many=True).data
        )

    def post(self, request, business_id):
        self.membership(request, business_id)
        serializer = PurchaseCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        purchase = self.run(
            create_purchase,
            business_id=business_id,
            supplier_id=str(d["supplier_id"]),
            purchase_number=d["purchase_number"],
            purchased_at=d["purchased_at"],
            notes=d["notes"],
            lines=_lines(d["lines"]),
            user=request.user,
        )
        return self.respond(business_id, purchase.pk, status.HTTP_201_CREATED)


class PurchaseDetailAPIView(PurchaseViewMixin, APIView):
    def get(self, request, business_id, pk):
        self.membership(request, business_id)
        try:
            return self.respond(business_id, pk)
        except SupplierPurchase.DoesNotExist as exc:
            raise NotFound("Not found.") from exc

    def patch(self, request, business_id, pk):
        self.membership(request, business_id)
        serializer = PurchaseUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = dict(serializer.validated_data)
        if "supplier_id" in d:
            d["supplier_id"] = str(d["supplier_id"])
        if "lines" in d:
            d["lines"] = _lines(d["lines"])

        self.run(
            update_draft_purchase,
            purchase_id=pk, business_id=business_id,
            user=request.user, **d,
        )
        return self.respond(business_id, pk)


class PurchaseReceiveAPIView(PurchaseViewMixin, APIView):
    def post(self, request, business_id, pk):
        self.membership(request, business_id)
        self.run(
            receive_purchase,
            purchase_id=pk, business_id=business_id, user=request.user,
        )
        return self.respond(business_id, pk)


class PurchaseCancelAPIView(PurchaseViewMixin, APIView):
    def post(self, request, business_id, pk):
        self.membership(request, business_id)
        serializer = PurchaseCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.run(
            cancel_draft_purchase,
            purchase_id=pk, business_id=business_id, user=request.user,
            reason=serializer.validated_data["reason"],
        )
        return self.respond(business_id, pk)


class PurchaseAdjustmentAPIView(PurchaseViewMixin, APIView):
    """GET: adjustment history. POST: record a return or a correction."""

    serializer_class = None  # set by subclasses
    service = None

    def post(self, request, business_id, pk):
        self.membership(request, business_id)
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        adjustment = self.run(
            self.service,
            business_id=business_id, purchase_id=pk, user=request.user,
            reason=d["reason"], client_ref=d.get("client_ref"),
            lines=[
                {**l, "purchase_line_id": str(l["purchase_line_id"])}
                for l in d["lines"]
            ],
        )
        return Response(
            PurchaseAdjustmentReadSerializer(adjustment).data,
            status=status.HTTP_201_CREATED,
        )


class PurchaseReturnAPIView(PurchaseAdjustmentAPIView):
    serializer_class = PurchaseReturnSerializer
    service = staticmethod(return_purchase_goods)


class PurchaseCorrectionAPIView(PurchaseAdjustmentAPIView):
    serializer_class = PurchaseCorrectionSerializer
    service = staticmethod(correct_purchase_entry)


class PurchaseAdjustmentListAPIView(PurchaseViewMixin, APIView):
    def get(self, request, business_id, pk):
        self.membership(request, business_id)
        try:
            purchase = self.queryset(business_id).get(pk=pk)
        except SupplierPurchase.DoesNotExist as exc:
            raise NotFound("Not found.") from exc
        adjustments = purchase.adjustments.prefetch_related("lines")
        return Response({
            "original_total": purchase.total_amount,
            "net_amount": purchase.net_amount,
            "adjustments": PurchaseAdjustmentReadSerializer(
                adjustments, many=True
            ).data,
        })
