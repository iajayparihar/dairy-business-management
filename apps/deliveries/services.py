
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.db.models import Q

from apps.businesses.models import Business
from apps.businesses.services import require_business_membership
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.products.models import Product, ProductPrice


@transaction.atomic
def create_delivery(
    *,
    business_id,
    customer_id,
    product_id,
    delivery_date,
    quantity,
    user,
    notes="",
):
    """
    Create a delivery using the selling price effective at the
    start of the delivery's local business date.

    The client cannot choose the unit price or line total.
    """

    # Verify that the caller is an active member of this business.
    require_business_membership(
        user=user,
        business_id=business_id,
    )

    business = Business.objects.get(
        pk=business_id,
        is_active=True,
    )

    # Lock the related records while creating the delivery.
    customer = Customer.objects.select_for_update().get(
        pk=customer_id,
        business=business,
        is_active=True,
    )

    product = Product.objects.select_for_update().get(
        pk=product_id,
        business=business,
        is_active=True,
    )

    if quantity is None:
        raise ValidationError({"quantity": "Quantity is required."})

    quantity = Decimal(str(quantity))

    if not quantity.is_finite() or quantity <= 0:
        raise ValidationError(
            {"quantity": "Quantity must be greater than zero."}
        )

    if not product.unit:
        raise ValidationError(
            {"product": "The product must have a valid unit."}
        )

    # Delivery dates are interpreted in the business's timezone.
    try:
        business_tz = ZoneInfo(business.timezone)
    except Exception as exc:
        raise ValidationError(
            {"business": "The business timezone is invalid."}
        ) from exc

    local_start = datetime.combine(delivery_date, time.min)
    effective_at = timezone.make_aware(
        local_start,
        business_tz,
    )

    # Select the most recent selling price that was effective
    # at the start of this local business date.
    price = (
        ProductPrice.objects.select_for_update()
        .filter(
            business=business,
            product=product,
            price_type=ProductPrice.PriceType.SELLING,
            effective_from__lte=effective_at,
        )
        .filter(
            # The interval is [effective_from, effective_to).
            # A null end date means the price remains effective.
            Q(effective_to__isnull=True)
            | Q(effective_to__gt=effective_at)
        )
        ).order_by("-effective_from").first()

    if price is None:
        raise ValidationError(
            {
                "product": (
                    "No selling price is configured for this "
                    "product on the delivery date."
                )
            }
        )

    return Delivery.objects.create(
        business=business,
        customer=customer,
        product=product,
        delivery_date=delivery_date,
        quantity=quantity,
        unit_price=price.unit_price,
        notes=notes,
        created_by=user,
    )
