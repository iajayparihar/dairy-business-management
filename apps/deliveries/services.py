from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import require_business_membership
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.inventory.models import StockMovement
from apps.inventory.services import STOCK_IN_TYPES, STOCK_OUT_TYPES
from apps.products.models import Product, ProductPrice

# Delivery boys (STAFF) may only record recent deliveries; owners and
# managers may backdate. Nobody may record a delivery in the future.
STAFF_BACKDATE_LIMIT = timedelta(hours=48)
CLOCK_SKEW = timedelta(minutes=5)
WHOLE_NUMBER_UNITS = {Product.Unit.PIECE, Product.Unit.PACKET}


@transaction.atomic
def create_delivery(
    *,
    business_id,
    customer_id,
    product_id,
    quantity,
    user,
    delivered_at=None,
    shift=Delivery.Shift.MORNING,
    unit_price=None,
    client_ref=None,
    notes="",
    replaces=None,
):
    """
    Record one product delivered to one customer in one shift.

    delivered_at defaults to now, must be timezone-aware and not in the
    future. The selling price effective at that exact timestamp is saved
    as a historical snapshot. unit_price is accepted only for open-price
    products; for everything else the client cannot choose prices.

    Re-sending the same client_ref returns the original delivery, so
    retries from a poor connection never double-charge.

    If stock tracking is on and stock is short, the delivery is still
    recorded (the milk has left) and the gap is stored in
    stock_shortfall for reconciliation.
    """

    membership = require_business_membership(
        user=user,
        business_id=business_id,
    )

    if delivered_at is None:
        delivered_at = timezone.now()

    if not isinstance(delivered_at, datetime):
        raise ValidationError({
            "delivered_at": "A valid delivery timestamp is required."
        })

    if not timezone.is_aware(delivered_at):
        raise ValidationError({
            "delivered_at": (
                "The delivery timestamp must include timezone information."
            )
        })

    delivered_at = delivered_at.astimezone(timezone.get_current_timezone())
    now = timezone.now()

    if delivered_at > now + CLOCK_SKEW:
        raise ValidationError({
            "delivered_at": "A delivery cannot be recorded in the future."
        })

    if (
        membership.role == BusinessMembership.Role.STAFF
        and delivered_at < now - STAFF_BACKDATE_LIMIT
    ):
        raise ValidationError({
            "delivered_at": (
                "Staff can only record deliveries from the last 48 hours."
            )
        })

    if shift not in Delivery.Shift.values:
        raise ValidationError({"shift": "Invalid shift."})

    try:
        business = Business.objects.get(
            pk=business_id,
            is_active=True,
            is_deleted=False,
        )
    except Business.DoesNotExist as exc:
        raise ValidationError({
            "business": "The business does not exist or is inactive."
        }) from exc

    # The customer lock serializes concurrent submissions for the same
    # customer, which makes the replay and duplicate checks below safe.
    try:
        customer = Customer.objects.select_for_update().get(
            pk=customer_id,
            business=business,
            is_active=True,
            is_deleted=False,
        )
    except Customer.DoesNotExist as exc:
        raise ValidationError({
            "customer": (
                "The customer does not exist, is inactive, "
                "or belongs to another business."
            )
        }) from exc

    # The product lock serializes the stock check per product.
    try:
        product = Product.objects.select_for_update().get(
            pk=product_id,
            business=business,
            is_active=True,
            is_deleted=False,
        )
    except Product.DoesNotExist as exc:
        raise ValidationError({
            "product": (
                "The product does not exist, is inactive, "
                "or belongs to another business."
            )
        }) from exc

    quantity = _validate_quantity(quantity, product)

    if client_ref:
        replay = Delivery.objects.filter(
            business=business,
            client_ref=client_ref,
        ).first()

        if replay is not None:
            if (replay.customer_id, replay.product_id) != (
                customer.pk,
                product.pk,
            ):
                raise ValidationError({
                    "client_ref": (
                        "This reference was used for another delivery."
                    )
                })
            return replay

    if Delivery.objects.filter(
        business=business,
        customer=customer,
        product=product,
        delivery_date=delivered_at.date(),
        shift=shift,
        status=Delivery.Status.DELIVERED,
    ).exists():
        raise ValidationError(
            "A delivery of this product is already recorded for this "
            "customer, date and shift. Cancel it to record a correction."
        )

    if product.allow_open_price:
        unit_price = _validate_open_price(unit_price)
    elif unit_price is not None:
        raise ValidationError({
            "unit_price": "The price of this product cannot be set manually."
        })
    else:
        unit_price = _effective_selling_price(
            business, product, delivered_at,
        )

    shortfall = Decimal("0.000")

    if product.track_stock:
        available = _get_stock_balance(
            business_id=business.pk,
            product_id=product.pk,
        )
        shortfall = max(
            quantity - max(available, Decimal("0")),
            Decimal("0"),
        )

    delivery = Delivery.objects.create(
        business=business,
        customer=customer,
        product=product,
        delivered_at=delivered_at,
        shift=shift,
        quantity=quantity,
        unit_price=unit_price,
        stock_shortfall=shortfall,
        client_ref=client_ref or None,
        notes=notes,
        created_by=user,
        replaces=replaces,
    )

    if product.track_stock:
        StockMovement.objects.create(
            business=business,
            product=product,
            movement_type=StockMovement.MovementType.DELIVERY,
            quantity=quantity,
            occurred_at=delivered_at,
            reason=f"Customer delivery {delivery.pk}",
            created_by=user,
            delivery=delivery,
        )

    return delivery


def _validate_quantity(quantity, product):
    if quantity is None:
        raise ValidationError({"quantity": "Quantity is required."})

    try:
        quantity = Decimal(str(quantity))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError({
            "quantity": "Enter a valid decimal quantity."
        }) from exc

    if not quantity.is_finite() or quantity <= Decimal("0"):
        raise ValidationError({
            "quantity": "Quantity must be greater than zero."
        })

    if quantity.as_tuple().exponent < -3:
        raise ValidationError({
            "quantity": "Quantity supports at most three decimal places."
        })

    if quantity >= Decimal("1000000000"):
        raise ValidationError({
            "quantity": "Quantity exceeds the supported maximum."
        })

    if (
        product.unit in WHOLE_NUMBER_UNITS
        and quantity != quantity.to_integral_value()
    ):
        raise ValidationError({
            "quantity": f"{product.get_unit_display()} must be a whole number."
        })

    return quantity


def _validate_open_price(unit_price):
    if unit_price is None:
        raise ValidationError({
            "unit_price": "This product requires a price per unit."
        })

    try:
        unit_price = Decimal(str(unit_price))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError({
            "unit_price": "Enter a valid price."
        }) from exc

    if (
        not unit_price.is_finite()
        or unit_price < 0
        or unit_price.as_tuple().exponent < -2
        or unit_price >= Decimal("10000000000")
    ):
        raise ValidationError({
            "unit_price": (
                "Price must be zero or more, with at most 2 decimals."
            )
        })

    return unit_price


def _effective_selling_price(business, product, at):
    """Selling price effective at `at`; periods are [from, to)."""
    price = (
        ProductPrice.objects.filter(
            business=business,
            product=product,
            price_type=ProductPrice.PriceType.SELLING,
            is_deleted=False,
            effective_from__lte=at,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=at))
        .order_by("-effective_from")
        .first()
    )

    if price is None:
        raise ValidationError({
            "product": (
                "No selling price is configured for this product "
                "at the delivery timestamp."
            )
        })

    return price.unit_price


def _get_stock_balance(*, business_id, product_id):
    totals = StockMovement.objects.filter(
        business_id=business_id,
        product_id=product_id,
    ).aggregate(
        stock_in=Sum("quantity", filter=Q(movement_type__in=STOCK_IN_TYPES)),
        stock_out=Sum("quantity", filter=Q(movement_type__in=STOCK_OUT_TYPES)),
    )

    return (totals["stock_in"] or Decimal("0.000")) - (
        totals["stock_out"] or Decimal("0.000")
    )
