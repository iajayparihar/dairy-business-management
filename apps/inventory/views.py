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
from apps.inventory.models import StockMovement
from apps.inventory.services import STOCK_IN_TYPES, stock_balance, stock_balances
from apps.products.models import Product
from apps.purchases.api_views import raise_api_validation_error
from apps.purchases.milk_queries import _date_param, local_day_bounds


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
