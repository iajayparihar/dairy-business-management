"""
Wastage and manual stock adjustments (OWNER/MANAGER).

Lock order (same as milk / sales, no deadlocks):
    business (no_key) -> adjustment -> product
A stock shortfall is recorded on the row and never blocks, like deliveries.
"""
import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.businesses.services import require_business_membership
from apps.deliveries.services import CLOCK_SKEW, _validate_quantity
from apps.inventory.models import StockAdjustment, StockMovement
from apps.inventory.services import stock_balance
from apps.payments.services import ClientRefConflict
from apps.products.models import Product
from apps.sales.services import (
    MANAGE_ROLES,
    _clean_reason,
    _lock_business,
)

MT = StockMovement.MovementType
KIND_TO_TYPE = {
    StockAdjustment.Kind.WASTAGE: MT.WASTAGE,
    StockAdjustment.Kind.LOSS: MT.ADJUSTMENT_OUT,
    StockAdjustment.Kind.GAIN: MT.ADJUSTMENT_IN,
}
OUT_KINDS = {StockAdjustment.Kind.WASTAGE, StockAdjustment.Kind.LOSS}
MIN_REASON_LENGTH = 3


def _clean_at(value):
    if value is None:
        return None
    if not isinstance(value, datetime) or not timezone.is_aware(value):
        raise ValidationError({
            "occurred_at": "A timestamp with timezone information is required."
        })
    return value


def _shortfall(balance, quantity):
    """Quantity beyond what was in stock (same formula as deliveries)."""
    return max(quantity - max(balance, Decimal("0")), Decimal("0"))


def _lock_product(business, product_id, *, require_active=True):
    manager = Product.objects if require_active else Product._base_manager
    filters = {"pk": product_id, "business": business}
    if require_active:
        filters.update(is_active=True, is_deleted=False)
    try:
        return manager.select_for_update().get(**filters)
    except (Product.DoesNotExist, ValueError, ValidationError) as exc:
        raise ValidationError({
            "product_id": (
                "The product does not exist, is inactive, "
                "or belongs to another business."
            )
        }) from exc


def _next_number(business, at):
    day = at.astimezone(ZoneInfo(business.timezone)).strftime("%y%m%d")
    prefix = f"ADJ-{day}-"
    seqs = [
        int(m.group(1))
        for n in StockAdjustment.objects.filter(
            business=business, adjustment_number__startswith=prefix
        ).values_list("adjustment_number", flat=True)
        if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", n))
    ]
    return f"{prefix}{max(seqs, default=0) + 1}"


def _clean_reason_text(reason):
    reason = _clean_reason(reason)
    if len(reason) < MIN_REASON_LENGTH:
        raise ValidationError({
            "reason": f"Give a reason of at least {MIN_REASON_LENGTH} characters."
        })
    return reason


def _same_request(adj, *, kind, product_id, quantity, reason, occurred_at):
    return (
        adj.kind == kind
        and str(adj.product_id) == str(product_id)
        and adj.quantity == quantity
        and adj.reason == reason
        and (occurred_at is None or adj.occurred_at == occurred_at)
    )


@transaction.atomic
def create_adjustment(
    *, business_id, user, kind, product_id, quantity, reason, client_ref,
    occurred_at=None,
):
    """
    Record wastage (out), a stock loss (out) or a stock gain (in).

    Retrying the same client_ref returns the original; the same ref with
    different data raises ClientRefConflict.
    """
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=MANAGE_ROLES
    )
    if kind not in StockAdjustment.Kind.values:
        raise ValidationError({"kind": "Invalid kind."})
    client_ref = (client_ref or "").strip()
    if not client_ref or len(client_ref) > 64:
        raise ValidationError(
            {"client_ref": "A client_ref of 1 to 64 characters is required."}
        )
    reason = _clean_reason_text(reason)
    explicit_at = _clean_at(occurred_at)

    business = _lock_business(business_id)
    product = _lock_product(business, product_id)
    if not product.track_stock:
        raise ValidationError({
            "product_id": "Stock tracking is not enabled for this product."
        })
    quantity = _validate_quantity(quantity, product)

    replay = StockAdjustment.objects.filter(
        business=business, client_ref=client_ref
    ).first()
    if replay is not None:
        if not _same_request(
            replay, kind=kind, product_id=product.pk, quantity=quantity,
            reason=reason, occurred_at=explicit_at,
        ):
            raise ClientRefConflict({
                "client_ref": (
                    "This reference was already used for a different "
                    "adjustment. Use a new reference."
                )
            })
        return replay

    at = explicit_at or timezone.now()
    if at > timezone.now() + CLOCK_SKEW:
        raise ValidationError(
            {"occurred_at": "An adjustment cannot be recorded in the future."}
        )

    shortfall = Decimal("0")
    if kind in OUT_KINDS:
        shortfall = _shortfall(
            stock_balance(business_id=business.pk, product_id=product.pk),
            quantity,
        )

    try:
        with transaction.atomic():
            adj = StockAdjustment.objects.create(
                business=business,
                adjustment_number=_next_number(business, at),
                product=product,
                kind=kind,
                quantity=quantity,
                occurred_at=at,
                reason=reason,
                client_ref=client_ref,
                stock_shortfall=shortfall,
                created_by=user,
            )
    except IntegrityError as exc:
        raise ValidationError(
            "Could not allocate an adjustment number. Please retry."
        ) from exc

    StockMovement.objects.create(
        business=business,
        product=product,
        movement_type=KIND_TO_TYPE[kind],
        quantity=quantity,
        occurred_at=at,
        reason=f"{adj.adjustment_number}: {reason}",
        created_by=user,
        stock_adjustment=adj,
    )
    return adj


@transaction.atomic
def cancel_adjustment(*, business_id, adjustment_id, user, reason):
    """Cancel an adjustment (keeps the row, posts a reversal at its time)."""
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=MANAGE_ROLES
    )
    reason = _clean_reason_text(reason)
    try:
        # The product of an adjustment never changes, so peeking is safe.
        product_id = StockAdjustment.objects.filter(
            pk=adjustment_id, business_id=business_id
        ).values_list("product_id", flat=True).get()
    except StockAdjustment.DoesNotExist as exc:
        raise ValidationError({"adjustment": "Adjustment not found."}) from exc

    business = _lock_business(business_id)
    adj = StockAdjustment.objects.select_for_update().get(
        pk=adjustment_id, business=business
    )
    product = _lock_product(business, product_id, require_active=False)

    if adj.status == StockAdjustment.Status.CANCELLED:
        raise ValidationError("This adjustment is already cancelled.")

    original = StockMovement.objects.get(
        stock_adjustment=adj, reversal_of__isnull=True
    )
    goes_out = original.movement_type in (MT.ADJUSTMENT_IN,)
    StockMovement.objects.create(
        business=business,
        product=product,
        movement_type=MT.REVERSAL_OUT if goes_out else MT.REVERSAL_IN,
        quantity=original.quantity,
        # Same business time as the original so the day nets to zero.
        occurred_at=original.occurred_at,
        reason=f"Cancelled {adj.adjustment_number}: {reason}",
        created_by=user,
        stock_adjustment=adj,
        reversal_of=original,
    )

    cancel_shortfall = Decimal("0")
    if goes_out:
        balance = stock_balance(business_id=business.pk, product_id=product.pk)
        if balance < 0:
            cancel_shortfall = min(-balance, original.quantity)

    adj.status = StockAdjustment.Status.CANCELLED
    adj.cancelled_at = timezone.now()
    adj.cancelled_by = user
    adj.cancel_reason = reason
    adj.cancel_stock_shortfall = cancel_shortfall
    adj.save(update_fields=[
        "status", "cancelled_at", "cancelled_by", "cancel_reason",
        "cancel_stock_shortfall",
    ])
    return adj
