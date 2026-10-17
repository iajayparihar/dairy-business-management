"""Shop sales API. Any member sells; cancel / correct are OWNER/MANAGER."""
import uuid

from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.common.pagination import StandardPagination
from apps.payments.services import ClientRefConflict
from apps.purchases.api_views import raise_api_validation_error
from apps.purchases.milk_queries import _choice_param, _date_param, local_day_bounds
from apps.sales import services
from apps.sales.models import Sale
from apps.sales.serializers import money, sale_queryset, serialize_sale

MANAGE = [BusinessMembership.Role.OWNER, BusinessMembership.Role.MANAGER]
CREATE_KEYS = {"client_ref", "customer_id", "payment_mode", "sold_at", "notes", "lines"}
CORRECT_KEYS = {"reason", "customer_id", "payment_mode", "notes", "lines"}


def fail(exc):
    """409 for a client_ref conflict, 400 for everything else."""
    if isinstance(exc, ClientRefConflict):
        return Response(exc.message_dict, status=status.HTTP_409_CONFLICT)
    raise_api_validation_error(exc)


def check_body(data, allowed, required=()):
    """Unknown or missing fields are a 400, never silently ignored."""
    if not isinstance(data, dict):
        raise DjangoValidationError({"detail": "Send a JSON object."})
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise DjangoValidationError(
            {k: "This field is not accepted." for k in unknown}
        )
    missing = [k for k in required if k not in data]
    if missing:
        raise DjangoValidationError({k: "This field is required." for k in missing})


def text(data, key, default=""):
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise DjangoValidationError({key: "Must be text."})
    return value


def uuid_field(data, key):
    value = data.get(key)
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise DjangoValidationError({key: "Must be a valid UUID."})


def datetime_field(data, key):
    value = data.get(key)
    if value is None:
        return None
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        raise DjangoValidationError({key: "Use an ISO 8601 date and time."})
    return parsed


class SalesView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessMember]
    manage_only = False

    def get_permissions(self):
        self.required_business_roles = MANAGE if self.manage_only else None
        return super().get_permissions()

    def sale(self, sale_id):
        return get_object_or_404(
            sale_queryset(self.request.business.id), pk=sale_id
        )

    def respond(self, sale_id, code=status.HTTP_200_OK):
        return Response(serialize_sale(self.sale(sale_id)), status=code)


class SaleListCreateView(SalesView):
    def get(self, request, business_id):
        params = request.query_params
        try:
            rows = sale_queryset(request.business.id)
            start, end = _date_param(params, "start_date"), _date_param(params, "end_date")
            if start and end and start > end:
                raise DjangoValidationError({"end_date": "Must be on or after start_date."})
            if start:
                rows = rows.filter(sold_at__gte=local_day_bounds(request.business, start)[0])
            if end:
                rows = rows.filter(sold_at__lt=local_day_bounds(request.business, end)[1])
            mode = _choice_param(params, "payment_mode", Sale.PaymentMode.values)
            state = _choice_param(params, "status", Sale.Status.values)
            if mode:
                rows = rows.filter(payment_mode=mode)
            if state:
                rows = rows.filter(status=state)
            customer = uuid_field(params, "customer_id")
            if customer:
                rows = rows.filter(customer_id=customer)
            if params.get("has_shortfall", "").lower() == "true":
                rows = rows.filter(lines__stock_shortfall__gt=0).distinct()
        except DjangoValidationError as exc:
            return fail(exc)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(rows, request)
        return paginator.get_paginated_response([serialize_sale(s) for s in page])

    def post(self, request, business_id):
        try:
            data = request.data
            check_body(data, CREATE_KEYS, required=("client_ref", "payment_mode", "lines"))
            sale = services.create_sale(
                business_id=request.business.id,
                user=request.user,
                payment_mode=text(data, "payment_mode"),
                lines=data["lines"],
                client_ref=text(data, "client_ref"),
                customer_id=uuid_field(data, "customer_id"),
                sold_at=datetime_field(data, "sold_at"),
                notes=text(data, "notes"),
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return self.respond(sale.pk, status.HTTP_201_CREATED)


class SaleDetailView(SalesView):
    def get(self, request, business_id, sale_id):
        return self.respond(sale_id)


class SaleCancelView(SalesView):
    manage_only = True

    def post(self, request, business_id, sale_id):
        try:
            check_body(request.data, {"reason"}, required=("reason",))
            services.cancel_sale(
                business_id=request.business.id, sale_id=self.sale(sale_id).pk,
                user=request.user, reason=text(request.data, "reason"),
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return self.respond(sale_id)


class SaleCorrectView(SalesView):
    manage_only = True

    def post(self, request, business_id, sale_id):
        try:
            data = request.data
            check_body(data, CORRECT_KEYS, required=("reason", "payment_mode", "lines"))
            new = services.correct_sale(
                business_id=request.business.id, sale_id=self.sale(sale_id).pk,
                user=request.user, reason=text(data, "reason"),
                payment_mode=text(data, "payment_mode"), lines=data["lines"],
                customer_id=uuid_field(data, "customer_id"),
                notes=text(data, "notes"),
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return self.respond(new.pk, status.HTTP_201_CREATED)


class SaleSummaryView(SalesView):
    def get(self, request, business_id):
        try:
            day = _date_param(request.query_params, "date")
            if day is None:
                raise DjangoValidationError({"date": "A date (YYYY-MM-DD) is required."})
            out = services.daily_summary(
                business_id=request.business.id, user=request.user, day=day
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return Response({
            "date": out["date"].isoformat(),
            "count": out["count"],
            "total": money(out["total"]),
            "by_payment_mode": {
                m: {"count": v["count"], "total": money(v["total"])}
                for m, v in out["by_payment_mode"].items()
            },
        })


class QuickCustomerView(SalesView):
    def post(self, request, business_id):
        try:
            check_body(request.data, {"name", "phone"}, required=("name", "phone"))
            customer = services.quick_create_customer(
                business_id=request.business.id, user=request.user,
                name=text(request.data, "name"), phone=text(request.data, "phone"),
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return Response(
            {"id": str(customer.pk), "customer_code": customer.customer_code,
             "name": customer.name, "phone": customer.phone},
            status=status.HTTP_201_CREATED,
        )
