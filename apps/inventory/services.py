
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q, Sum

from apps.businesses.services import require_business_membership
from apps.inventory.models import StockMovement
from apps.products.models import Product


STOCK_IN_TYPES = [
    StockMovement.MovementType.PURCHASE_RECEIPT,
    StockMovement.MovementType.ADJUSTMENT_IN,
    StockMovement.MovementType.REVERSAL_IN,
]

STOCK_OUT_TYPES = [
    StockMovement.MovementType.DELIVERY,
    StockMovement.MovementType.WASTAGE,
    StockMovement.MovementType.ADJUSTMENT_OUT,
    StockMovement.MovementType.REVERSAL_OUT,
]


def get_stock_balance(*, business_id, product_id, user):
    require_business_membership(
        user=user,
        business_id=business_id,
    )

    product = Product.objects.get(
        pk=product_id,
        business_id=business_id,
    )

    if not product.track_stock:
        raise ValidationError(
            "Stock tracking is not enabled for this product."
        )

    totals = StockMovement.objects.filter(
        business_id=business_id,
        product_id=product.pk,
    ).aggregate(
        stock_in=Sum(
            "quantity",
            filter=Q(movement_type__in=STOCK_IN_TYPES),
        ),
        stock_out=Sum(
            "quantity",
            filter=Q(movement_type__in=STOCK_OUT_TYPES),
        ),
    )

    stock_in = totals["stock_in"] or Decimal("0.000")
    stock_out = totals["stock_out"] or Decimal("0.000")

    return stock_in - stock_out
