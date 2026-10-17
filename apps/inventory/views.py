"""Stock balance API. Any member may read quantities (no money here)."""
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.businesses.permissions import IsBusinessMember
from apps.common.pagination import StandardPagination
from apps.inventory import adjustments, production
from apps.inventory.models import (
    ProductionBatch, ProductionLine, StockAdjustment, StockMovement,
)
from apps.inventory.services import STOCK_IN_TYPES, stock_balance, stock_balances
from apps.products.models import Product
from apps.purchases.api_views import raise_api_validation_error
from apps.purchases.milk_queries import _choice_param, _date_param, local_day_bounds
from apps.sales.views import MANAGE, check_body, datetime_field, fail, text, uuid_field
from rest_framework import status


def qty(value):
    return f"{value:.3f}"


def serialize_balance(product, balance):
    return {
        "product_id": str(product.pk),
        "sku": product.sku,
        "name": product.name,
        "unit": product.unit,
        "balance": qty(balance),
        "is_negative": balance < 0,
    }


def serialize_movement(m):
    return {
        "id": str(m.pk),
        "movement_type": m.movement_type,
        "direction": "IN" if m.movement_type in STOCK_IN_TYPES else "OUT",
        "quantity": qty(m.quantity),
        "occurred_at": m.occurred_at.isoformat(),
        "created_at": m.created_at.isoformat(),
        "reason": m.reason,
        "reversal_of": str(m.reversal_of_id) if m.reversal_of_id else None,
    }


class StockView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessMember]


class StockBalanceListView(StockView):
    def get(self, request, business_id):
        params = request.query_params
        balances = stock_balances(business_id=request.business.id)
        products = Product.objects.filter(
            business_id=request.business.id, track_stock=True
        ).order_by("name", "sku")
        search = params.get("search", "").strip()
        if search:
            products = products.filter(
                Q(name__icontains=search) | Q(sku__icontains=search)
            )
        if params.get("negative", "").lower() == "true":
            products = products.filter(
                pk__in=[p for p, b in balances.items() if b < 0]
            )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(products, request)
        return paginator.get_paginated_response([
            serialize_balance(p, balances.get(p.pk, Decimal("0"))) for p in page
        ])


class StockBalanceDetailView(StockView):
    def get(self, request, business_id, product_id):
        product = get_object_or_404(
            Product, pk=product_id, business_id=request.business.id, track_stock=True
        )
        try:
            params = request.query_params
            start, end = _date_param(params, "from"), _date_param(params, "to")
            if start and end and start > end:
                raise DjangoValidationError({"to": "Must be on or after from."})
        except DjangoValidationError as exc:
            return raise_api_validation_error(exc)
        moves = StockMovement.objects.filter(
            business_id=request.business.id, product=product
        )
        if start:
            moves = moves.filter(occurred_at__gte=local_day_bounds(request.business, start)[0])
        if end:
            moves = moves.filter(occurred_at__lt=local_day_bounds(request.business, end)[1])
        paginator = StandardPagination()
        page = paginator.paginate_queryset(moves, request)
        response = paginator.get_paginated_response(
            [serialize_movement(m) for m in page]
        )
        response.data.update(serialize_balance(
            product,
            stock_balance(business_id=request.business.id, product_id=product.pk),
        ))
        return response


# ------------------------------------------------ wastage / adjustments

ADJUSTMENT_KEYS = {"client_ref", "kind", "product_id", "quantity", "reason", "occurred_at"}


def serialize_adjustment(a):
    return {
        "id": str(a.pk),
        "adjustment_number": a.adjustment_number,
        "kind": a.kind,
        "status": a.status,
        "product_id": str(a.product_id),
        "product_name": a.product.name,
        "unit": a.product.unit,
        "quantity": qty(a.quantity),
        "occurred_at": a.occurred_at.isoformat(),
        "reason": a.reason,
        "client_ref": a.client_ref,
        "stock_shortfall": qty(a.stock_shortfall),
        "has_shortfall": a.stock_shortfall > 0 or a.cancel_stock_shortfall > 0,
        "cancel_stock_shortfall": qty(a.cancel_stock_shortfall),
        "cancelled_at": a.cancelled_at.isoformat() if a.cancelled_at else None,
        "cancel_reason": a.cancel_reason,
        "created_at": a.created_at.isoformat(),
    }


class AdjustmentView(StockView):
    """Wastage and adjustments are OWNER/MANAGER only."""

    def get_permissions(self):
        self.required_business_roles = MANAGE
        return super().get_permissions()

    def queryset(self):
        return StockAdjustment.objects.filter(
            business_id=self.request.business.id
        ).select_related("product")

    def respond(self, adjustment_id, code=status.HTTP_200_OK):
        adj = get_object_or_404(self.queryset(), pk=adjustment_id)
        return Response(serialize_adjustment(adj), status=code)


class AdjustmentListCreateView(AdjustmentView):
    def get(self, request, business_id):
        params = request.query_params
        try:
            rows = self.queryset()
            start, end = _date_param(params, "from"), _date_param(params, "to")
            if start and end and start > end:
                raise DjangoValidationError({"to": "Must be on or after from."})
            if start:
                rows = rows.filter(occurred_at__gte=local_day_bounds(request.business, start)[0])
            if end:
                rows = rows.filter(occurred_at__lt=local_day_bounds(request.business, end)[1])
            kind = _choice_param(params, "kind", StockAdjustment.Kind.values)
            state = _choice_param(params, "status", StockAdjustment.Status.values)
            if kind:
                rows = rows.filter(kind=kind)
            if state:
                rows = rows.filter(status=state)
            product = uuid_field(params, "product_id")
            if product:
                rows = rows.filter(product_id=product)
            if params.get("has_shortfall", "").lower() == "true":
                rows = rows.filter(
                    Q(stock_shortfall__gt=0) | Q(cancel_stock_shortfall__gt=0)
                )
        except DjangoValidationError as exc:
            return fail(exc)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(rows, request)
        return paginator.get_paginated_response([serialize_adjustment(a) for a in page])

    def post(self, request, business_id):
        try:
            data = request.data
            check_body(data, ADJUSTMENT_KEYS,
                       required=("client_ref", "kind", "product_id", "quantity", "reason"))
            adj = adjustments.create_adjustment(
                business_id=request.business.id,
                user=request.user,
                kind=text(data, "kind"),
                product_id=uuid_field(data, "product_id"),
                quantity=data["quantity"],
                reason=text(data, "reason"),
                client_ref=text(data, "client_ref"),
                occurred_at=datetime_field(data, "occurred_at"),
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return self.respond(adj.pk, status.HTTP_201_CREATED)


class AdjustmentDetailView(AdjustmentView):
    def get(self, request, business_id, adjustment_id):
        return self.respond(adjustment_id)


class AdjustmentCancelView(AdjustmentView):
    def post(self, request, business_id, adjustment_id):
        try:
            check_body(request.data, {"reason"}, required=("reason",))
            adj = get_object_or_404(self.queryset(), pk=adjustment_id)
            adjustments.cancel_adjustment(
                business_id=request.business.id, adjustment_id=adj.pk,
                user=request.user, reason=text(request.data, "reason"),
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return self.respond(adjustment_id)


# ------------------------------------------------------ production

BATCH_KEYS = {"client_ref", "produced_at", "notes", "inputs", "outputs"}
BATCH_CORRECT_KEYS = {"reason", "notes", "inputs", "outputs"}
LITRE = "L"


def serialize_line(line):
    body = {
        "product_id": str(line.product_id),
        "product_name": line.product.name,
        "unit": line.unit,
        "quantity": qty(line.quantity),
    }
    if line.kind == ProductionLine.Kind.INPUT:
        body["stock_shortfall"] = qty(line.stock_shortfall)
    return body


def serialize_batch(b):
    lines = list(b.lines.all())
    inputs = [l for l in lines if l.kind == ProductionLine.Kind.INPUT]
    outputs = [l for l in lines if l.kind == ProductionLine.Kind.OUTPUT]
    # Yield is display only: shown when everything used is measured in litres.
    litres = sum((l.quantity for l in inputs), Decimal("0"))
    all_litres = bool(inputs) and all(l.unit == LITRE for l in inputs)
    out = []
    for l in outputs:
        row = serialize_line(l)
        row["per_100_litres"] = (
            f"{l.quantity * 100 / litres:.3f}" if all_litres and litres else None
        )
        out.append(row)
    replacement = getattr(b, "replaced_by", None)
    return {
        "id": str(b.pk),
        "batch_number": b.batch_number,
        "status": b.status,
        "produced_at": b.produced_at.isoformat(),
        "notes": b.notes,
        "client_ref": b.client_ref,
        "inputs": [serialize_line(l) for l in inputs],
        "input_litres": qty(litres) if all_litres else None,
        "outputs": out,
        "has_shortfall": any(l.stock_shortfall > 0 for l in inputs),
        "cancel_has_shortfall": b.cancel_has_shortfall,
        "replaces": str(b.replaces_id) if b.replaces_id else None,
        "replaced_by": str(replacement.pk) if replacement else None,
        "cancelled_at": b.cancelled_at.isoformat() if b.cancelled_at else None,
        "cancel_reason": b.cancel_reason,
        "created_at": b.created_at.isoformat(),
    }


class ProductionView(StockView):
    """Production is OWNER/MANAGER only."""

    def get_permissions(self):
        self.required_business_roles = MANAGE
        return super().get_permissions()

    def queryset(self):
        return ProductionBatch.objects.filter(
            business_id=self.request.business.id
        ).select_related("replaced_by").prefetch_related("lines__product")

    def respond(self, batch_id, code=status.HTTP_200_OK):
        batch = get_object_or_404(self.queryset(), pk=batch_id)
        return Response(serialize_batch(batch), status=code)


class ProductionListCreateView(ProductionView):
    def get(self, request, business_id):
        params = request.query_params
        try:
            rows = self.queryset()
            start, end = _date_param(params, "from"), _date_param(params, "to")
            if start and end and start > end:
                raise DjangoValidationError({"to": "Must be on or after from."})
            if start:
                rows = rows.filter(produced_at__gte=local_day_bounds(request.business, start)[0])
            if end:
                rows = rows.filter(produced_at__lt=local_day_bounds(request.business, end)[1])
            state = _choice_param(params, "status", ProductionBatch.Status.values)
            if state:
                rows = rows.filter(status=state)
            product = uuid_field(params, "product_id")
            if product:
                rows = rows.filter(lines__product_id=product).distinct()
            if params.get("has_shortfall", "").lower() == "true":
                rows = rows.filter(
                    Q(lines__stock_shortfall__gt=0) | Q(cancel_has_shortfall=True)
                ).distinct()
        except DjangoValidationError as exc:
            return fail(exc)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(rows, request)
        return paginator.get_paginated_response([serialize_batch(b) for b in page])

    def post(self, request, business_id):
        try:
            data = request.data
            check_body(data, BATCH_KEYS, required=("client_ref", "inputs", "outputs"))
            batch = production.create_batch(
                business_id=request.business.id,
                user=request.user,
                inputs=data["inputs"],
                outputs=data["outputs"],
                client_ref=text(data, "client_ref"),
                produced_at=datetime_field(data, "produced_at"),
                notes=text(data, "notes"),
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return self.respond(batch.pk, status.HTTP_201_CREATED)


class ProductionDetailView(ProductionView):
    def get(self, request, business_id, batch_id):
        return self.respond(batch_id)


class ProductionCancelView(ProductionView):
    def post(self, request, business_id, batch_id):
        try:
            check_body(request.data, {"reason"}, required=("reason",))
            batch = get_object_or_404(self.queryset(), pk=batch_id)
            production.cancel_batch(
                business_id=request.business.id, batch_id=batch.pk,
                user=request.user, reason=text(request.data, "reason"),
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return self.respond(batch_id)


class ProductionCorrectView(ProductionView):
    def post(self, request, business_id, batch_id):
        try:
            data = request.data
            check_body(data, BATCH_CORRECT_KEYS, required=("reason", "inputs", "outputs"))
            batch = get_object_or_404(self.queryset(), pk=batch_id)
            new = production.correct_batch(
                business_id=request.business.id, batch_id=batch.pk,
                user=request.user, reason=text(data, "reason"),
                inputs=data["inputs"], outputs=data["outputs"],
                notes=text(data, "notes"),
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return self.respond(new.pk, status.HTTP_201_CREATED)
