"""
Production batches: input products used up -> output products made.

Lock order (same as milk / sales / adjustments, no deadlocks):
    business (no_key) -> batch -> products (sorted by id)
A stock shortfall on an input is recorded on the line and never blocks.
Units are not converted and there are no recipes: yield is display only.
"""
import re
import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.businesses.services import require_business_membership
from apps.deliveries.services import CLOCK_SKEW, _validate_quantity
from apps.inventory.models import ProductionBatch, ProductionLine, StockMovement
from apps.inventory.services import stock_balance
from apps.payments.services import ClientRefConflict
from apps.sales.services import (
    MANAGE_ROLES,
    _clean_reason,
    _lock_business,
    _lock_products,
)



def _clean_notes(notes):
    notes = notes or ""
    if len(notes) > 1000:
        raise ValidationError({"notes": "At most 1000 characters."})
    return notes


def _lock_all(business, product_ids, *, require_active=True):
    """_lock_products, with an error keyed to this API's fields."""
    try:
        return _lock_products(business, product_ids, require_active=require_active)
    except ValidationError as exc:
        raise ValidationError({"inputs": exc.message_dict["lines"]}) from exc

MT = StockMovement.MovementType
INPUT, OUTPUT = ProductionLine.Kind.INPUT, ProductionLine.Kind.OUTPUT
MAX_LINES = 20
LINE_KEYS = {"product_id", "quantity"}


def _clean_at(value):
    if value is None:
        return None
    if not isinstance(value, datetime) or not timezone.is_aware(value):
        raise ValidationError({
            "produced_at": "A timestamp with timezone information is required."
        })
    return value


def _clean_side(lines, field):
    """Shape check, no database: [{product_id, quantity}], no repeats."""
    if not isinstance(lines, (list, tuple)) or not lines:
        raise ValidationError({field: "At least one line is required."})
    if len(lines) > MAX_LINES:
        raise ValidationError({field: f"At most {MAX_LINES} lines."})
    clean, seen = [], set()
    for i, line in enumerate(lines, start=1):
        if not isinstance(line, dict) or set(line) != LINE_KEYS:
            raise ValidationError({
                field: f"Line {i}: send exactly product_id and quantity."
            })
        try:
            pid = str(uuid.UUID(str(line["product_id"])))
        except ValueError:
            raise ValidationError({field: f"Line {i}: product_id must be a UUID."})
        if pid in seen:
            raise ValidationError({
                field: f"Line {i}: repeats a product; add the quantities together."
            })
        seen.add(pid)
        clean.append({"product_id": pid, "quantity": line["quantity"]})
    return clean


def _clean_sides(inputs, outputs):
    inputs, outputs = _clean_side(inputs, "inputs"), _clean_side(outputs, "outputs")
    both = {l["product_id"] for l in inputs} & {l["product_id"] for l in outputs}
    if both:
        raise ValidationError({
            "outputs": "A product cannot be both used and made in one batch."
        })
    return inputs, outputs


def _check_products(products, inputs, outputs):
    """Every product active (a new batch) and tracked, named in the error."""
    for field, side in (("inputs", inputs), ("outputs", outputs)):
        for i, line in enumerate(side, start=1):
            p = products[line["product_id"]]
            if not p.is_active or p.is_deleted:
                raise ValidationError({field: f"Line {i}: {p.name} is inactive."})
            if not p.track_stock:
                raise ValidationError({
                    field: f"Line {i}: stock tracking is not enabled for {p.name}."
                })


def _next_number(business, at):
    day = at.astimezone(ZoneInfo(business.timezone)).strftime("%y%m%d")
    prefix = f"PRD-{day}-"
    seqs = [
        int(m.group(1))
        for n in ProductionBatch.objects.filter(
            business=business, batch_number__startswith=prefix
        ).values_list("batch_number", flat=True)
        if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", n))
    ]
    return f"{prefix}{max(seqs, default=0) + 1}"


def _signature(inputs, outputs, products):
    return sorted(
        (kind, pid, _validate_quantity(l["quantity"], products[pid]))
        for kind, side in ((INPUT, inputs), (OUTPUT, outputs))
        for l in side
        for pid in [l["product_id"]]
    )


def _batch_signature(batch):
    return sorted(
        (l.kind, str(l.product_id), l.quantity) for l in batch.lines.all()
    )


def _insert_batch(
    *, business, products, user, inputs, outputs, produced_at, client_ref,
    notes, replaces=None, enforce_window=True,
):
    """Write a batch. Caller holds business and product locks."""
    if enforce_window and produced_at > timezone.now() + CLOCK_SKEW:
        raise ValidationError(
            {"produced_at": "A batch cannot be recorded in the future."}
        )

    rows = []
    for kind, side in ((INPUT, inputs), (OUTPUT, outputs)):
        for line in side:
            product = products[line["product_id"]]
            quantity = _validate_quantity(line["quantity"], product)
            shortfall = Decimal("0")
            if kind == INPUT:
                balance = stock_balance(business_id=business.pk, product_id=product.pk)
                shortfall = max(quantity - max(balance, Decimal("0")), Decimal("0"))
            rows.append((kind, product, quantity, shortfall))

    try:
        with transaction.atomic():  # savepoint keeps the outer txn usable
            batch = ProductionBatch.objects.create(
                business=business,
                batch_number=_next_number(business, produced_at),
                produced_at=produced_at,
                notes=notes,
                client_ref=client_ref or None,
                replaces=replaces,
                created_by=user,
            )
    except IntegrityError as exc:
        raise ValidationError(
            "A conflicting batch or client reference was just recorded. "
            "Refresh and retry with a new reference."
        ) from exc

    for kind, product, quantity, shortfall in rows:
        line = ProductionLine.objects.create(
            business=business, batch=batch, kind=kind, product=product,
            quantity=quantity, unit=product.unit, stock_shortfall=shortfall,
        )
        StockMovement.objects.create(
            business=business,
            product=product,
            movement_type=MT.PRODUCTION_OUT if kind == INPUT else MT.PRODUCTION_IN,
            quantity=quantity,
            occurred_at=produced_at,
            reason=f"Production {batch.batch_number}",
            created_by=user,
            production_line=line,
        )
    return batch


@transaction.atomic
def create_batch(
    *, business_id, user, inputs, outputs, client_ref, produced_at=None, notes="",
):
    """
    Record a production run (OWNER/MANAGER).

    Retrying the same client_ref returns the original batch; the same ref
    with different data raises ClientRefConflict.
    """
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=MANAGE_ROLES
    )
    client_ref = (client_ref or "").strip()
    if not client_ref or len(client_ref) > 64:
        raise ValidationError(
            {"client_ref": "A client_ref of 1 to 64 characters is required."}
        )
    notes = _clean_notes(notes)
    explicit_at = _clean_at(produced_at)
    inputs, outputs = _clean_sides(inputs, outputs)

    business = _lock_business(business_id)
    products = _lock_all(
        business, [l["product_id"] for l in inputs + outputs]
    )

    replay = ProductionBatch.objects.filter(
        business=business, client_ref=client_ref
    ).first()
    if replay is not None:
        same = (
            replay.notes == notes
            and (explicit_at is None or replay.produced_at == explicit_at)
            and _batch_signature(replay) == _signature(inputs, outputs, products)
        )
        if not same:
            raise ClientRefConflict({
                "client_ref": (
                    "This reference was already used for a different batch. "
                    "Use a new reference for a new batch."
                )
            })
        return replay

    _check_products(products, inputs, outputs)
    return _insert_batch(
        business=business, products=products, user=user, inputs=inputs,
        outputs=outputs, produced_at=explicit_at or timezone.now(),
        client_ref=client_ref, notes=notes,
    )


# ------------------------------------------------- cancel / correct

def _lock_batch(business_id, batch_id, *, extra_product_ids=()):
    """Business -> batch -> products (own lines + extras, sorted by id)."""
    # A batch's lines never change, so peeking is safe.
    own = list(
        ProductionLine.objects.filter(
            batch_id=batch_id, business_id=business_id
        ).values_list("product_id", flat=True)
    )
    if not own:
        raise ValidationError({"batch": "Batch not found."})

    business = _lock_business(business_id)
    batch = ProductionBatch.objects.select_for_update().get(
        pk=batch_id, business=business
    )
    products = _lock_all(
        business, own + list(extra_product_ids), require_active=False
    )
    return business, batch, products


def _cancel_locked(batch, *, business, user, reason):
    if batch.status == ProductionBatch.Status.CANCELLED:
        raise ValidationError("This batch is already cancelled.")

    removed_outputs = []
    for original in StockMovement.objects.filter(
        production_line__batch=batch, reversal_of__isnull=True,
        movement_type__in=[MT.PRODUCTION_IN, MT.PRODUCTION_OUT],
    ):
        made = original.movement_type == MT.PRODUCTION_IN
        StockMovement.objects.create(
            business=business,
            product_id=original.product_id,
            movement_type=MT.REVERSAL_OUT if made else MT.REVERSAL_IN,
            quantity=original.quantity,
            # Same business time as the original so the day nets to zero.
            occurred_at=original.occurred_at,
            reason=f"Cancelled {batch.batch_number}: {reason}",
            created_by=user,
            production_line=original.production_line,
            reversal_of=original,
        )
        if made:
            removed_outputs.append(original.product_id)

    batch.cancel_has_shortfall = any(
        stock_balance(business_id=business.pk, product_id=pid) < 0
        for pid in removed_outputs
    )
    batch.status = ProductionBatch.Status.CANCELLED
    batch.cancelled_at = timezone.now()
    batch.cancelled_by = user
    batch.cancel_reason = reason
    batch.save(update_fields=[
        "status", "cancelled_at", "cancelled_by", "cancel_reason",
        "cancel_has_shortfall",
    ])


@transaction.atomic
def cancel_batch(*, business_id, batch_id, user, reason):
    """Cancel a batch (OWNER/MANAGER); keeps the row and reverses stock."""
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=MANAGE_ROLES
    )
    reason = _clean_reason(reason)
    business, batch, _products = _lock_batch(business_id, batch_id)
    _cancel_locked(batch, business=business, user=user, reason=reason)
    return batch


@transaction.atomic
def correct_batch(
    *, business_id, batch_id, user, reason, inputs, outputs, notes="",
):
    """Cancel a batch and record the replacement at the original time."""
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=MANAGE_ROLES
    )
    reason = _clean_reason(reason)
    inputs, outputs = _clean_sides(inputs, outputs)
    business, old, products = _lock_batch(
        business_id, batch_id,
        extra_product_ids=[l["product_id"] for l in inputs + outputs],
    )
    _check_products(products, inputs, outputs)

    _cancel_locked(old, business=business, user=user, reason=reason)
    return _insert_batch(
        business=business, products=products, user=user, inputs=inputs,
        outputs=outputs, produced_at=old.produced_at, client_ref=None,
        notes=_clean_notes(notes), replaces=old, enforce_window=False,
    )
