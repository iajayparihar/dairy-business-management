
import uuid

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404

from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.common.pagination import StandardPagination
from apps.purchases.api_serializers import (
    MilkCollectionCancelSerializer,
    MilkCollectionCorrectSerializer,
    MilkCollectionCreateSerializer,
    MilkCollectionReadSerializer,
    MilkConfigSerializer,
)
from apps.purchases.milk_queries import (
    _date_param,
    filter_milk_collections,
    milk_daily_summary,
)
from apps.purchases.models import MilkCollection
from django.db.models import Exists, OuterRef

from apps.inventory.models import BusinessMilkConfig, StockMovement
from apps.inventory.services import set_milk_config
from apps.purchases.services import (
    ClientRefConflict,
    cancel_milk_collection,
    correct_milk_collection,
    create_milk_collection,
)
from apps.suppliers.models import Supplier


def with_stock_posted(queryset):
    return queryset.annotate(stock_posted=Exists(
        StockMovement.objects.filter(
            milk_collection=OuterRef("pk"),
            movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
        )
    ))


def raise_api_validation_error(exc):
    detail = getattr(exc, "message_dict", None)
    if detail is None:
        detail = exc.messages
    raise ValidationError(detail)


class MilkCollectionPermissionMixin:
    """Milk entries carry cost data: OWNER/MANAGER only, reads included."""

    permission_classes = [IsAuthenticated, IsBusinessMember]

    def get_permissions(self):
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


class MilkCollectionListCreateAPIView(
    MilkCollectionPermissionMixin,
    generics.ListCreateAPIView,
):
    pagination_class = StandardPagination

    def get_queryset(self):
        supplier = self.get_supplier()

        queryset = with_stock_posted(MilkCollection.objects.filter(
            business_id=self.request.business.id,
            supplier_id=supplier.id,
        ).select_related("rate_record", "purchase"))

        try:
            queryset = filter_milk_collections(
                queryset, self.request.query_params, self.request.business,
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)

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
                milk_type=data["milk_type"],
                collected_at=data["collected_at"],
                shift=data.get("shift"),
                client_ref=data.get("client_ref"),
                litres=data["litres"],
                fat=data.get("fat"),
                user=request.user,
            )
        except ClientRefConflict as exc:
            return Response(
                exc.message_dict, status=status.HTTP_409_CONFLICT,
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)
        except IntegrityError:
            # Database constraints remain the final duplicate guard.
            raise ValidationError(
                "This milk entry conflicts with an existing record. "
                "Please retry."
            )

        output = MilkCollectionReadSerializer(collection)
        return Response(
            output.data,
            status=(
                status.HTTP_200_OK if collection.replayed
                else status.HTTP_201_CREATED
            ),
        )


class MilkCollectionDetailAPIView(
    MilkCollectionPermissionMixin,
    generics.RetrieveAPIView,
):
    serializer_class = MilkCollectionReadSerializer
    lookup_url_kwarg = "collection_id"

    def get_queryset(self):
        supplier = self.get_supplier()

        return with_stock_posted(MilkCollection.objects.filter(
            business_id=self.request.business.id,
            supplier_id=supplier.id,
        ).select_related("rate_record", "purchase"))


class BusinessMilkCollectionListAPIView(
    MilkCollectionPermissionMixin,
    generics.ListAPIView,
):
    """Every supplier's entries for the business (filters + pagination)."""

    serializer_class = MilkCollectionReadSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        queryset = with_stock_posted(MilkCollection.objects.filter(
            business_id=self.request.business.id,
        ).select_related("rate_record", "purchase"))

        supplier = self.request.query_params.get("supplier")
        if supplier:
            try:
                supplier = uuid.UUID(supplier)
            except ValueError:
                raise ValidationError({"supplier": "Enter a valid id."})
            queryset = queryset.filter(supplier_id=supplier)

        try:
            queryset = filter_milk_collections(
                queryset, self.request.query_params, self.request.business,
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)

        return queryset.order_by("-collected_at", "-created_at")


class MilkDailySummaryAPIView(MilkCollectionPermissionMixin, APIView):
    def get(self, request, business_id):
        try:
            day = _date_param(request.query_params, "date")
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)
        if day is None:
            raise ValidationError({"date": "Required, use YYYY-MM-DD."})

        supplier_id = request.query_params.get("supplier")
        if supplier_id:
            try:
                supplier_id = uuid.UUID(supplier_id)
            except ValueError:
                raise ValidationError({"supplier": "Enter a valid id."})

        try:
            summary = milk_daily_summary(
                business=request.business, day=day,
                supplier_id=supplier_id or None,
            )
        except Supplier.DoesNotExist:
            raise NotFound("Supplier not found.")

        return Response(summary)


class MilkCollectionActionMixin(MilkCollectionPermissionMixin):
    """Cancel/correct: the entry must belong to this supplier + business."""

    def get_collection(self):
        supplier = self.get_supplier()
        return get_object_or_404(
            MilkCollection.objects.filter(
                business_id=self.request.business.id,
                supplier_id=supplier.id,
            ),
            pk=self.kwargs["collection_id"],
        )

    def read(self, collection_id):
        return MilkCollectionReadSerializer(
            with_stock_posted(MilkCollection.objects.filter(
                pk=collection_id,
            )).get()
        ).data


class MilkCollectionCancelAPIView(MilkCollectionActionMixin, APIView):
    def post(self, request, **kwargs):
        collection = self.get_collection()
        serializer = MilkCollectionCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            cancel_milk_collection(
                business_id=request.business.id,
                collection_id=collection.id,
                user=request.user,
                reason=serializer.validated_data["reason"],
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)

        return Response(self.read(collection.id))


class MilkCollectionCorrectAPIView(MilkCollectionActionMixin, APIView):
    def post(self, request, **kwargs):
        collection = self.get_collection()
        serializer = MilkCollectionCorrectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            new = correct_milk_collection(
                business_id=request.business.id,
                collection_id=collection.id,
                user=request.user,
                reason=data["reason"],
                litres=data["litres"],
                fat=data.get("fat"),
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)

        return Response({
            "collection": self.read(new.id),
            "cancelled_collection": self.read(collection.id),
            "stock_shortfall": new.replaces.cancel_stock_shortfall,
        })


class BusinessMilkConfigAPIView(APIView):
    """Which products hold raw cow / buffalo milk stock (OWNER/MANAGER)."""

    permission_classes = [IsAuthenticated, IsBusinessMember]

    def get_permissions(self):
        self.required_business_roles = [
            BusinessMembership.Role.OWNER,
            BusinessMembership.Role.MANAGER,
        ]
        return super().get_permissions()

    @staticmethod
    def _body(config):
        return {
            "cow_product": config and config.cow_product_id,
            "buffalo_product": config and config.buffalo_product_id,
        }

    def get(self, request, business_id):
        config = BusinessMilkConfig.objects.filter(
            business_id=request.business.id,
        ).first()
        return Response(self._body(config))

    def put(self, request, business_id):
        serializer = MilkConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            config = set_milk_config(
                business_id=request.business.id,
                cow_product_id=data["cow_product"],
                buffalo_product_id=data["buffalo_product"],
                user=request.user,
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)

        return Response(self._body(config))


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
            status=(
                status.HTTP_200_OK if getattr(adjustment, "replayed", False)
                else status.HTTP_201_CREATED
            ),
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
            "original_total": str(purchase.total_amount),
            "net_amount": str(purchase.net_amount),
            "adjustments": PurchaseAdjustmentReadSerializer(
                adjustments, many=True
            ).data,
        })
