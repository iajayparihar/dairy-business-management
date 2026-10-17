
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import require_business_membership
from apps.products.models import Product, ProductPrice


@transaction.atomic
def change_product_price(
    *,
    business_id,
    product_id,
    price_type,
    unit_price,
    effective_from,
    user,
):
    """
    Create a price period for a product in a business.

    Rules:
    - Only active business owners and managers can change prices.
    - Effective timestamps must be timezone-aware.
    - Past price changes are not allowed through this service.
    - The previous price ends exactly when the new price starts.
    - The new price ends at the next scheduled price, if one exists.
    - Otherwise, the new price remains open-ended (effective_to=None).
    - Existing transaction price snapshots are never modified.
    """

    # 1. Authorize the user.
    membership = require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=[
            BusinessMembership.Role.OWNER,
            BusinessMembership.Role.MANAGER,
        ],
    )

    # 2. Load the active business.
    try:
        business = Business.objects.get(
            pk=membership.business_id,
            is_active=True,
            is_deleted=False,
        )
    except Business.DoesNotExist as exc:
        raise ValidationError(
            "The business does not exist or is inactive."
        ) from exc

    # 3. Validate the effective timestamp. Omitted means "from now".
    now = timezone.now()
    if effective_from is None:
        effective_from = now

    if not isinstance(effective_from, datetime):
        raise ValidationError({
            "effective_from": "A valid timestamp is required."
        })

    if not timezone.is_aware(effective_from):
        raise ValidationError({
            "effective_from": (
                "The timestamp must include timezone information."
            )
        })

    # Use Django's configured timezone, Asia/Kolkata.
    effective_from = effective_from.astimezone(
        timezone.get_current_timezone()
    )

    # Do not allow retroactive price changes.
    if effective_from < now:
        raise ValidationError({
            "effective_from": (
                "Price changes cannot be backdated. "
                "Choose the current or a future timestamp."
            )
        })

    # 4. Validate the price type.
    try:
        price_type = ProductPrice.PriceType(price_type)
    except (ValueError, TypeError):
        raise ValidationError({
            "price_type": "Invalid price type."
        })

    # 5. Validate the price amount.
    try:
        amount = Decimal(str(unit_price))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError({
            "unit_price": "A valid decimal price is required."
        })

    if not amount.is_finite() or amount < Decimal("0.00"):
        raise ValidationError({
            "unit_price": (
                "Price must be a finite, non-negative amount."
            )
        })

    if amount.as_tuple().exponent < -2:
        raise ValidationError({
            "unit_price": (
                "Price supports at most two decimal places."
            )
        })

    # Match the ProductPrice model's DecimalField precision.
    if amount >= Decimal("10000000000"):
        raise ValidationError({
            "unit_price": "The price exceeds the supported maximum."
        })

    # 6. Lock the product row to serialize price changes
    # for this product. All price-writing operations should use
    # this service or an equivalent locking strategy.
    
    try:
        product = Product.objects.select_for_update().get(
            pk=product_id,
            business=business,
            is_active=True,
            is_deleted=False,
        )
    except Product.DoesNotExist as exc:
        raise ValidationError({
            "product": "The product does not exist, is inactive, "
                       "or belongs to another business."
        }) from exc

    # 7. Load price history for this product and price type.
    price_queryset = ProductPrice.objects.filter(
        business=business,
        product=product,
        price_type=price_type,
        is_deleted=False,
    )

    # Reject duplicate effective start timestamps.
    if price_queryset.filter(
        effective_from=effective_from
    ).exists():
        raise ValidationError({
            "effective_from": (
                "A price period already starts at this timestamp. "
                "Choose another timestamp or use an audited correction."
            )
        })

    # 8. Find the period that contains the new effective timestamp.
    # Period boundaries follow [effective_from, effective_to).
    current_price = (
        price_queryset.filter(
            effective_from__lt=effective_from,
        )
        .filter(
            Q(effective_to__isnull=True)
            | Q(effective_to__gt=effective_from)
        )
        .order_by("-effective_from")
        .first()
    )

    # 9. Find the next scheduled price.
    next_price = (
        price_queryset.filter(
            effective_from__gt=effective_from,
        )
        .order_by("effective_from")
        .first()
    )

    # 10. Close the previous period at the exact change timestamp.
    if current_price is not None:
        current_price.effective_to = effective_from
        current_price.save(
            update_fields=["effective_to", "updated_at"]
        )

    # 11. End the new period at the next scheduled price's start.
    # If there is no next scheduled price, keep it open-ended.
    new_effective_to = (
        next_price.effective_from
        if next_price is not None
        else None
    )

    # 12. Create the new price period.
    # A database exclusion constraint should reject any overlap.
    try:
        new_price = ProductPrice.objects.create(
            business=business,
            product=product,
            price_type=price_type,
            unit_price=amount,
            effective_from=effective_from,
            effective_to=new_effective_to,
            created_by=user,
        )
    except IntegrityError as exc:
        raise ValidationError({
            "effective_from": (
                "This price conflicts with an existing price period. "
                "Refresh the price history and try again."
            )
        }) from exc

    return new_price


def get_product_price(
    *,
    business_id,
    product_id,
    price_type,
    at=None,
):
    """
    Return the price effective at a specific timestamp.

    If `at` is omitted, use the current time.

    The effective period is:
        effective_from <= at < effective_to

    If effective_to is NULL, the period has no scheduled end.
    """

    if at is None:
        at = timezone.now()

    if not isinstance(at, datetime) or not timezone.is_aware(at):
        raise ValidationError({
            "at": "The requested timestamp must be timezone-aware."
        })

    at = at.astimezone(timezone.get_current_timezone())

    try:
        price_type = ProductPrice.PriceType(price_type)
    except (ValueError, TypeError):
        raise ValidationError({
            "price_type": "Invalid price type."
        })

    price = (
        ProductPrice.objects.filter(
            business_id=business_id,
            product_id=product_id,
            price_type=price_type,
            is_deleted=False,
            effective_from__lte=at,
        )
        .filter(
            Q(effective_to__isnull=True)
            | Q(effective_to__gt=at)
        )
        .order_by("-effective_from")
        .first()
    )

    if price is None:
        raise ValidationError(
            "No price is configured for this product "
            "at the requested timestamp."
        )

    return price
