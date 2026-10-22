"""
Returns and entry-mistake corrections for RECEIVED purchases.

The purchase, its lines and its stock receipts are never edited. Each
operation appends an immutable PurchaseAdjustment (the payable change) and,
for tracked products, an append-only StockMovement. Every operation locks the
purchase row first, which serializes returns, corrections and payment
allocations on that purchase.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.businesses.services import require_business_membership
from apps.inventory.models import StockMovement
from apps.purchases.models import (
    PurchaseAdjustment,
    PurchaseAdjustmentLine,
    PurchaseLine,
    SupplierPurchase,
)
from apps.purchases.services import ALLOWED_ROLES, _decimal, _lock_purchase

CENT = Decimal("0.01")


def _total(quantity, unit_price):
    return (quantity * unit_price).quantize(CENT, rounding=ROUND_HALF_UP)


def _current_state(line):
    """(quantity, unit_price) after all earlier adjustments."""
    last = (
        line.adjustment_lines.order_by("-created_at", "-pk").first()
    )
    if last is None:
        return line.quantity, line.unit_price
    return last.quantity_after, last.unit_price_after


def _qty(value, field):
    number = _decimal(value, field)
    if number <= 0 or number.as_tuple().exponent < -3:
        raise ValidationError(
            {field: "Must be positive with at most 3 decimals."}
        )
    return number


def _price(value, field):
    number = _decimal(value, field)
    if number < 0 or number.as_tuple().exponent < -4:
        raise ValidationError(
            {field: "Must be non-negative with at most 4 decimals."}
        )
    return number


def _same_request(earlier, purchase, kind, reason, lines, ids):
    if (earlier.purchase_id, earlier.adjustment_type, earlier.reason) != (purchase.pk, kind, reason):
        return False
    stored = {str(l.purchase_line_id): l for l in earlier.lines.all()}
    if set(stored) != set(ids):
        return False
    for item, line_id in zip(lines, ids):
        row = stored[line_id]
        try:
            if kind == PurchaseAdjustment.Type.RETURN:
                if row.quantity_before - row.quantity_after != _qty(item.get("quantity"), "quantity"):
                    return False
            else:
                if item.get("quantity") is not None and row.quantity_after != _qty(item["quantity"], "quantity"):
                    return False
                if item.get("unit_price") is not None and row.unit_price_after != _price(item["unit_price"], "unit_price"):
                    return False
        except ValidationError:
            return False
    return True


def _post(*, business_id, purchase_id, user, reason, lines, client_ref, kind):
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=ALLOWED_ROLES,
    )

    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    if not lines:
        raise ValidationError({"lines": "At least one line is required."})

    purchase = _lock_purchase(purchase_id, business_id)

    if purchase.status != SupplierPurchase.Status.RECEIVED:
        raise ValidationError(
            "Only received purchases can be returned or corrected."
        )

    ids = [str(item["purchase_line_id"]) for item in lines]
    if len(set(ids)) != len(ids):
        raise ValidationError("A purchase line can appear only once.")

    client_ref = (client_ref or "").strip() or None
    earlier = (
        PurchaseAdjustment.objects.filter(business_id=business_id, client_ref=client_ref)
        .prefetch_related("lines").first()
        if client_ref else None
    )
    if earlier is not None:
        # Retry after a lost response: the same request returns the first result.
        if not _same_request(earlier, purchase, kind, reason, lines, ids):
            raise ValidationError(
                {"client_ref": "This client_ref was already used for a different request."}
            )
        earlier.replayed = True
        return earlier

    purchase_lines = {
        str(l.pk): l
        for l in PurchaseLine.objects.filter(
            purchase=purchase, business_id=business_id, pk__in=ids
        ).select_related("product")
    }
    if len(purchase_lines) != len(ids):
        raise ValidationError("Line does not belong to this purchase.")

    now = timezone.now()
    plan = []
    for item, line_id in zip(lines, ids):
        line = purchase_lines[line_id]
        qty_before, price_before = _current_state(line)
        qty_after, price_after = qty_before, price_before

        if kind == PurchaseAdjustment.Type.RETURN:
            returned = _qty(item.get("quantity"), "quantity")
            if returned > qty_before:
                raise ValidationError({
                    "quantity": (
                        f"Cannot return {returned} of {line.product.name}; "
                        f"only {qty_before} remain."
                    )
                })
            qty_after = qty_before - returned
        else:
            if item.get("quantity") is None and item.get("unit_price") is None:
                raise ValidationError(
                    "Provide a corrected quantity and/or unit price."
                )
            if item.get("quantity") is not None:
                qty_after = _qty(item["quantity"], "quantity")
            if item.get("unit_price") is not None:
                price_after = _price(item["unit_price"], "unit_price")
            if (qty_after, price_after) == (qty_before, price_before):
                raise ValidationError(
                    f"No change for {line.product.name}."
                )

        plan.append((
            line, qty_before, qty_after, price_before, price_after,
            _total(qty_after, price_after) - _total(qty_before, price_before),
        ))

    adjustment = PurchaseAdjustment.objects.create(
        business_id=business_id,
        purchase=purchase,
        adjustment_type=kind,
        reason=reason,
        amount=sum((p[5] for p in plan), Decimal("0.00")),
        occurred_at=now,
        client_ref=client_ref,
        created_by=user,
    )

    for line, qty_before, qty_after, price_before, price_after, amount in plan:
        movement = _post_stock(
            purchase, line, qty_after - qty_before, now, user, reason, kind
        )
        PurchaseAdjustmentLine.objects.create(
            adjustment=adjustment,
            business_id=business_id,
            purchase_line=line,
            quantity_before=qty_before,
            quantity_after=qty_after,
            unit_price_before=price_before,
            unit_price_after=price_after,
            amount=amount,
            stock_movement=movement,
        )

    return adjustment


def _post_stock(purchase, line, delta, when, user, reason, kind):
    """Stock effect of a quantity change; only if the receipt was stocked."""
    if delta == 0:
        return None

    receipt = line.stock_movements.filter(
        movement_type=StockMovement.MovementType.PURCHASE_RECEIPT
    ).first()
    if receipt is None:
        return None  # product was not stock-tracked when received

    label = "Return" if kind == PurchaseAdjustment.Type.RETURN else "Correction"
    kwargs = dict(
        business_id=purchase.business_id,
        product_id=line.product_id,
        quantity=abs(delta),
        occurred_at=when,
        reason=f"{label} on purchase {purchase.purchase_number}: {reason}",
        created_by=user,
        purchase_line=line,
    )
    if delta < 0:
        return StockMovement.objects.create(
            movement_type=StockMovement.MovementType.REVERSAL_OUT,
            reversal_of=receipt,
            **kwargs,
        )
    return StockMovement.objects.create(
        movement_type=StockMovement.MovementType.ADJUSTMENT_IN, **kwargs
    )


@transaction.atomic
def return_purchase_goods(
    *, business_id, purchase_id, user, reason, lines, client_ref=None
):
    """lines: [{"purchase_line_id", "quantity"}] - quantity being returned."""
    return _post(
        business_id=business_id, purchase_id=purchase_id, user=user,
        reason=reason, lines=lines, client_ref=client_ref,
        kind=PurchaseAdjustment.Type.RETURN,
    )


@transaction.atomic
def correct_purchase_entry(
    *, business_id, purchase_id, user, reason, lines, client_ref=None
):
    """lines: [{"purchase_line_id", "quantity"?, "unit_price"?}] - the
    corrected (new) values, not deltas."""
    return _post(
        business_id=business_id, purchase_id=purchase_id, user=user,
        reason=reason, lines=lines, client_ref=client_ref,
        kind=PurchaseAdjustment.Type.CORRECTION,
    )
