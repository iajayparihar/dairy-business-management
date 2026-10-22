from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.shortcuts import get_object_or_404

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.businesses.permissions import IsBusinessMember
from apps.common.dates import parse_iso_date
from apps.common.pagination import StandardPagination
from apps.common.serializers import decimals_to_str
from apps.suppliers.models import Supplier
from apps.payments.statements import get_supplier_account_statement

from django.db import transaction
from apps.businesses.models import BusinessMembership
from apps.payments.api_serializers import (
    PaymentAllocateSerializer,
    PaymentReverseSerializer,
    SupplierPaymentCreateSerializer,
    SupplierPaymentReadSerializer,
    serialize_allocation,
    serialize_reversal,
)
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocationReversal,
)
from apps.purchases.api_views import raise_api_validation_error
from apps.payments.pay_summary import get_pay_summary
from apps.payments.selectors import get_payment_amounts, get_supplier_balance
from apps.payments.services import (
    ClientRefConflict,
    allocate_supplier_advance,
    record_supplier_payment,
    reverse_supplier_payment,
)

class OwnerManagerMixin:
    """Money data (purchases, payments, statements): OWNER/MANAGER only."""

    permission_classes = [IsAuthenticated, IsBusinessMember]

    def get_permissions(self):
        self.required_business_roles = [
            BusinessMembership.Role.OWNER,
            BusinessMembership.Role.MANAGER,
        ]
        return super().get_permissions()


def _build_statement(request, supplier_id, start_date=None, end_date=None):
    supplier = get_object_or_404(
        Supplier.objects.filter(
            business_id=request.business.id, is_deleted=False
        ),
        pk=supplier_id,
    )
    try:
        return get_supplier_account_statement(
            business_id=request.business.id,
            supplier_id=supplier.id,
            start_date=start_date or request.query_params.get("start_date"),
            end_date=end_date or request.query_params.get("end_date"),
        )
    except DjangoValidationError as exc:
        raise_api_validation_error(exc)


class SupplierAccountStatementAPIView(OwnerManagerMixin, APIView):
    """
    GET ?start_date=&end_date=&page=&page_size= (default 50, max 200).
    `transactions` holds only the requested page; every row's running
    `balance` is computed over the whole period, so pages line up and the
    last row of the last page equals `closing_balance`. Totals are for the
    whole period. `pagination` = {count, page, page_size, num_pages}.
    """

    def get(self, request, business_id, supplier_id):
        statement = _build_statement(request, supplier_id)

        paginator = StandardPagination()
        page = paginator.paginate_queryset(
            statement["transactions"], request
        )
        statement["transactions"] = page
        statement["pagination"] = {
            "count": paginator.page.paginator.count,
            "page": paginator.page.number,
            "page_size": paginator.get_page_size(request),
            "num_pages": paginator.page.paginator.num_pages,
        }
        return Response(decimals_to_str(statement), status=status.HTTP_200_OK)


class SupplierAccountStatementPrintAPIView(OwnerManagerMixin, APIView):
    """
    GET ?start_date=&end_date= -> print-friendly HTML (all rows, no
    pagination). Defaults to the last 30 days; at most 366 days. It is an
    authenticated API call: the frontend fetches it with the bearer token
    and shows/prints the result (the browser cannot send the header itself).
    """

    MAX_DAYS = 366
    DEFAULT_DAYS = 30

    def get(self, request, business_id, supplier_id):
        params = request.query_params
        tz = ZoneInfo(request.business.timezone)
        today = timezone.now().astimezone(tz).date()

        try:
            end = parse_iso_date(params.get("end_date") or today.isoformat())
            start = parse_iso_date(
                params.get("start_date")
                or (end - timedelta(days=self.DEFAULT_DAYS - 1)).isoformat()
            )
        except ValueError:
            raise ValidationError({"detail": "Use YYYY-MM-DD dates."})

        if start > end:
            raise ValidationError(
                {"end_date": "End date must be on or after start date."}
            )
        if (end - start).days + 1 > self.MAX_DAYS:
            raise ValidationError(
                f"The period can be at most {self.MAX_DAYS} days."
            )

        statement = _build_statement(request, supplier_id, start, end)

        rows = [
            {**t, "local_time": t["timestamp"].astimezone(tz)}
            for t in statement["transactions"]
        ]
        html = render_to_string(
            "payments/statement_print.html",
            {
                "s": statement,
                "rows": rows,
                "generated_local": statement["generated_at"].astimezone(tz),
            },
        )
        response = HttpResponse(html, content_type="text/html; charset=utf-8")
        response["Cache-Control"] = "no-store"
        return response


class SupplierPaymentListCreateAPIView(OwnerManagerMixin, APIView):

    def get(self, request, business_id, supplier_id):
        supplier = get_object_or_404(
            Supplier.objects.filter(
                business_id=request.business.id,
                is_deleted=False,
            ),
            pk=supplier_id,
        )

        payments = SupplierPayment.objects.filter(
            business_id=request.business.id,
            supplier=supplier,
        ).order_by("-paid_at", "-created_at")

        paginator = StandardPagination()
        page = paginator.paginate_queryset(payments, request)
        return paginator.get_paginated_response(
            SupplierPaymentReadSerializer(page, many=True).data
        )

    @transaction.atomic
    def post(self, request, business_id, supplier_id):
        supplier = get_object_or_404(
            Supplier.objects.filter(
                business_id=request.business.id,
                is_active=True,
                is_deleted=False,
            ),
            pk=supplier_id,
        )

        serializer = SupplierPaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            payment = record_supplier_payment(
                business_id=request.business.id,
                supplier_id=supplier.pk,
                payment_number=data["payment_number"],
                paid_at=data["paid_at"],
                amount=data["amount"],
                payment_method=data["payment_method"],
                reference_number=data["reference_number"],
                notes=data["notes"],
                auto_allocate=True,
                client_ref=data.get("client_ref"),
                user=request.user,
            )
        except ClientRefConflict as exc:
            return Response(
                exc.message_dict, status=status.HTTP_409_CONFLICT
            )
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None)
            if detail is None:
                detail = exc.messages
            raise ValidationError(detail)

        return Response(
            SupplierPaymentReadSerializer(payment).data,
            status=(
                status.HTTP_200_OK
                if payment.replayed
                else status.HTTP_201_CREATED
            ),
        )



class OwnerManagerPaymentView(OwnerManagerMixin, APIView):

    def get_payment(self, request, payment_id):
        # Tenant-scoped: another business's payment is simply a 404.
        return get_object_or_404(
            SupplierPayment.objects.filter(business_id=request.business.id),
            pk=payment_id,
        )

    def payment_detail(self, payment):
        reversals = list(payment.reversals.select_related("created_by"))
        unwinds_by_reversal = {}
        for unwind in (
            SupplierPaymentAllocationReversal.objects
            .filter(reversal__payment=payment)
            .select_related("allocation__purchase")
            .order_by("created_at", "pk")
        ):
            unwinds_by_reversal.setdefault(unwind.reversal_id, []).append(unwind)

        allocations = (
            payment.allocations.select_related("purchase")
            .prefetch_related("unwinds")
            .order_by("created_at", "pk")
        )

        return decimals_to_str({
            **SupplierPaymentReadSerializer(payment).data,
            "amounts": get_payment_amounts(payment),
            "allocations": [serialize_allocation(a) for a in allocations],
            "reversals": [
                serialize_reversal(r, unwinds_by_reversal.get(r.id, []))
                for r in reversals
            ],
        })


class SupplierPaymentDetailAPIView(OwnerManagerPaymentView):
    def get(self, request, business_id, payment_id):
        payment = self.get_payment(request, payment_id)
        return Response(self.payment_detail(payment))


class SupplierPaymentReverseAPIView(OwnerManagerPaymentView):
    """
    POST {amount?, reason, client_ref?}
    201 new reversal, 200 identical client_ref replay, 409 client_ref reused
    for different data, 400 invalid (excessive amount, already fully
    reversed, missing reason). Response: the reversal plus the payment detail.
    """

    def post(self, request, business_id, payment_id):
        payment = self.get_payment(request, payment_id)

        serializer = PaymentReverseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            reversal = reverse_supplier_payment(
                payment_id=payment.pk,
                business_id=request.business.id,
                amount=data.get("amount"),
                reason=data["reason"],
                client_ref=data.get("client_ref"),
                user=request.user,
            )
        except ClientRefConflict as exc:
            return Response(exc.message_dict, status=status.HTTP_409_CONFLICT)
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)

        payment.refresh_from_db()
        return Response(
            {
                "reversal": decimals_to_str(serialize_reversal(reversal, reversal.unwound)),
                "payment": self.payment_detail(payment),
            },
            status=(
                status.HTTP_200_OK
                if reversal.replayed
                else status.HTTP_201_CREATED
            ),
        )


class SupplierPaymentAllocateAPIView(OwnerManagerPaymentView):
    """
    POST {purchase_id, amount}
    201 allocated, 200 identical allocation already exists (retry),
    400 invalid (exceeds remaining advance or purchase outstanding,
    other supplier, not received, already allocated with a different
    amount). Response: the allocation plus the payment detail.
    """

    def post(self, request, business_id, payment_id):
        payment = self.get_payment(request, payment_id)

        serializer = PaymentAllocateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            allocation = allocate_supplier_advance(
                payment_id=payment.pk,
                business_id=request.business.id,
                purchase_id=data["purchase_id"],
                amount=data["amount"],
                user=request.user,
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)

        return Response(
            {
                "allocation": decimals_to_str(serialize_allocation(allocation)),
                "payment": self.payment_detail(payment),
            },
            status=(
                status.HTTP_200_OK
                if allocation.replayed
                else status.HTTP_201_CREATED
            ),
        )


class SupplierBalanceAPIView(OwnerManagerPaymentView):
    """GET: outstanding (not clamped, negative = refund due) and advance."""

    def get(self, request, business_id, supplier_id):
        supplier = get_object_or_404(
            Supplier.objects.filter(
                business_id=request.business.id, is_deleted=False
            ),
            pk=supplier_id,
        )
        return Response(decimals_to_str({
            "supplier_id": str(supplier.pk),
            **get_supplier_balance(
                business_id=request.business.id, supplier_id=supplier.pk
            ),
        }))


class SupplierPaySummaryAPIView(OwnerManagerPaymentView):
    """
    GET ?days=N (1-366, default 5, includes today) or
        ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD (both, inclusive).
    days together with dates, or only one date, is a 400. Read-only: the
    suggested_amount is advice; POST .../payments/ re-checks live data.
    """

    def get(self, request, business_id, supplier_id):
        supplier = get_object_or_404(
            Supplier.objects.filter(
                business_id=request.business.id, is_deleted=False
            ),
            pk=supplier_id,
        )
        params = request.query_params

        try:
            summary = get_pay_summary(
                business_id=request.business.id,
                supplier_id=supplier.pk,
                days=params.get("days"),
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)

        return Response(decimals_to_str(summary))
