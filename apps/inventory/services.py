
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q, Sum

from apps.businesses.services import require_business_membership
from django.db import transaction

from apps.inventory.models import BusinessMilkConfig, StockMovement
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

    return stock_balance(business_id=business_id, product_id=product.pk)


def stock_balance(*, business_id, product_id):
    """Net stock from the ledger; no permission check (service-internal)."""
    totals = StockMovement.objects.filter(
        business_id=business_id,
        product_id=product_id,
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


def stock_net_by_date(*, business_id, product_id, basis, date_from, date_to):
    """
    Net stock change per Asia/Kolkata date, from the append-only ledger.

    basis="business": group by occurred_at, the date the stock really
        moved. Corrections net out on the original date. Use for daily
        milk balance and production reports.
    basis="posted": group by created_at, the date each entry was written.
        Shows corrections on the day they were made. Use for audit.

    Both views always sum to the same total.
    """
    from django.db.models.functions import TruncDate
    from django.utils import timezone

    field = {"business": "occurred_at", "posted": "created_at"}[basis]
    rows = (
        StockMovement.objects.filter(
            business_id=business_id,
            product_id=product_id,
        )
        .annotate(day=TruncDate(field, tzinfo=timezone.get_current_timezone()))
        .filter(day__gte=date_from, day__lte=date_to)
        .values("day")
        .annotate(
            stock_in=Sum("quantity", filter=Q(movement_type__in=STOCK_IN_TYPES)),
            stock_out=Sum("quantity", filter=Q(movement_type__in=STOCK_OUT_TYPES)),
        )
    )

    return {
        r["day"]: (r["stock_in"] or Decimal("0")) - (r["stock_out"] or Decimal("0"))
        for r in rows
    }


def milk_product_for(business_id, milk_type):
    """The mapped, still-valid stock product for a milk type, else None."""
    config = (
        BusinessMilkConfig.objects.filter(business_id=business_id)
        .select_related("cow_product", "buffalo_product")
        .first()
    )
    if config is None:
        return None

    product = (
        config.cow_product if milk_type == "COW" else config.buffalo_product
    )
    if (
        product is None
        or not product.is_active
        or product.is_deleted
        or not product.track_stock
    ):
        return None
    return product


@transaction.atomic
def set_milk_config(*, business_id, cow_product_id, buffalo_product_id, user):
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=["OWNER", "MANAGER"],
    )

    if cow_product_id and cow_product_id == buffalo_product_id:
        raise ValidationError(
            "Cow and buffalo milk must use different products."
        )

    def load(product_id, field):
        if product_id is None:
            return None
        product = Product.objects.filter(
            pk=product_id,
            business_id=business_id,
            is_active=True,
            is_deleted=False,
        ).first()
        if product is None:
            raise ValidationError({
                field: "Product not found, inactive, or in another business."
            })
        if not product.track_stock:
            raise ValidationError({
                field: "Stock tracking must be enabled for this product."
            })
        if product.unit != Product.Unit.LITRE:
            raise ValidationError({
                field: "Milk products must be measured in litres."
            })
        return product

    cow = load(cow_product_id, "cow_product")
    buffalo = load(buffalo_product_id, "buffalo_product")

    config, _ = BusinessMilkConfig.objects.update_or_create(
        business_id=business_id,
        defaults={
            "cow_product": cow,
            "buffalo_product": buffalo,
            "updated_by": user,
        },
    )
    return config
