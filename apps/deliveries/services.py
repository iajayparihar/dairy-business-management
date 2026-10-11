
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

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
    delivered_at,
    quantity,
    user,
    notes="",
):
    """
    Create a delivery using the selling price effective at the
    exact delivery timestamp.

    The timestamp must be timezone-aware. Django's configured
    timezone is used for local-time interpretation.

    The client cannot choose unit_price or line_total.
    The selected unit price is saved as a historical snapshot.
    """

    # 1. Verify business membership.
    require_business_membership(
        user=user,
        business_id=business_id,
    )

    # 2. Validate the delivery timestamp.
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

    # Normalize the timestamp to Django's configured timezone.
    delivered_at = delivered_at.astimezone(
        timezone.get_current_timezone()
    )

    # Derive the business date from the exact timestamp.
    delivery_date = delivered_at.date()

    # 3. Load the active business.
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

    # 4. Lock and validate the customer.
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

    # 5. Lock and validate the product.
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

    # 6. Validate quantity.
    if quantity is None:
        raise ValidationError({
            "quantity": "Quantity is required."
        })

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

    # 7. Find the selling price effective at delivery time.
    # Price periods follow [effective_from, effective_to).
    price = (
        ProductPrice.objects.select_for_update()
        .filter(
            business=business,
            product=product,
            price_type=ProductPrice.PriceType.SELLING,
            is_deleted=False,
            effective_from__lte=delivered_at,
        )
        .filter(
            Q(effective_to__isnull=True)
            | Q(effective_to__gt=delivered_at)
        )
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

    # 8. Create the delivery with the selected price snapshot.
    # Delivery.save() calculates line_total.
    return Delivery.objects.create(
        business=business,
        customer=customer,
        product=product,
        delivery_date=delivery_date,
        delivered_at=delivered_at,
        quantity=quantity,
        unit_price=price.unit_price,
        notes=notes,
        created_by=user,
    )
