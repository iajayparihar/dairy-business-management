"""
Owner-only correction of a milk rate that was entered wrong for a past period.

Nothing is edited or deleted: the wrong rate row is CANCELLED (kept, with who / when / why),
the right rate is recorded as new rows, and every milk entry of that supplier and milk type
inside the period is corrected at its ORIGINAL time and shift (old entry cancelled, new entry
priced at the right rate), so the supplier statement and balance follow. Two steps: a preview
that writes nothing, then a confirm that must repeat the previewed count and total.

Lock order: business (no_key) -> supplier (no_key) -> rate rows -> purchase -> collection.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.businesses.models import Business
from apps.businesses.services import require_business_membership
from apps.purchases.models import MilkCollection
from apps.purchases.services import correct_milk_collection
from apps.suppliers.models import Supplier, SupplierMilkRate
from apps.suppliers.services import calculate_milk_amount

MAX_DAYS = 31


def _entry_row(c, new_rate):
    fat = None if c.milk_type == MilkCollection.MilkType.COW else c.fat
    new_amount = calculate_milk_amount(
        litres=c.litres, rate=new_rate, milk_type=c.milk_type, fat=fat
    )
    return {
        "collection_id": str(c.pk),
        "collection_number": c.collection_number,
        "collected_at": c.collected_at.isoformat(),
        "shift": c.shift,
        "litres": c.litres,
        "fat": None if fat is None else c.fat,
        "old_rate": c.applied_rate,
        "old_amount": c.amount,
        "new_rate": new_rate,
        "new_amount": new_amount,
        "difference": new_amount - c.amount,
    }


@transaction.atomic
def correct_supplier_milk_rate(
    *, business_id, supplier_id, user, milk_type, effective_from, effective_to,
    rate, reason, confirm=False, expected_count=None, expected_new_total=None,
):
    require_business_membership(user=user, business_id=business_id, allowed_roles=["OWNER"])

    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    if len(reason) > 500:
        raise ValidationError({"reason": "At most 500 characters."})
    if milk_type not in SupplierMilkRate.MilkType.values:
        raise ValidationError({"milk_type": "Unsupported milk type."})
    if timezone.is_naive(effective_from) or timezone.is_naive(effective_to):
        raise ValidationError("Timestamps must include a timezone.")
    if effective_to <= effective_from:
        raise ValidationError({"effective_to": "Must be later than effective_from."})
    if effective_to - effective_from > timedelta(days=MAX_DAYS):
        raise ValidationError({"effective_to": f"A correction covers at most {MAX_DAYS} days."})
    if effective_to > timezone.now() + timedelta(minutes=5):
        raise ValidationError({"effective_to": "Only a period that has already passed can be corrected."})
    rate = Decimal(str(rate))
    if not rate.is_finite() or rate <= 0:
        raise ValidationError({"rate": "The rate must be greater than zero."})

    Business.objects.select_for_update(no_key=True).get(pk=business_id)
    supplier = Supplier.objects.select_for_update(no_key=True).get(
        pk=supplier_id, business_id=business_id, is_deleted=False
    )

    old_rates = list(
        SupplierMilkRate.objects.select_for_update().filter(
            business_id=business_id, supplier=supplier, milk_type=milk_type,
            cancelled_at__isnull=True, effective_from__lt=effective_to,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=effective_from))
        .order_by("effective_from")
    )
    entries = list(
        MilkCollection.objects.filter(
            business_id=business_id, supplier=supplier, milk_type=milk_type,
            status=MilkCollection.Status.RECORDED,
            collected_at__gte=effective_from, collected_at__lt=effective_to,
        ).order_by("collected_at", "pk")
    )
    rows = [_entry_row(c, rate) for c in entries]
    old_total = sum((r["old_amount"] for r in rows), Decimal("0.00"))
    new_total = sum((r["new_amount"] for r in rows), Decimal("0.00"))
    preview = {
        "confirmed": False,
        "milk_type": milk_type,
        "effective_from": effective_from.isoformat(),
        "effective_to": effective_to.isoformat(),
        "new_rate": rate,
        "rates_cancelled": [
            {"id": str(r.pk), "rate": r.rate, "effective_from": r.effective_from.isoformat(),
             "effective_to": r.effective_to.isoformat() if r.effective_to else None}
            for r in old_rates
        ],
        "count": len(rows),
        "old_total": old_total,
        "new_total": new_total,
        "difference": new_total - old_total,
        "entries": rows,
    }
    if not confirm:
        return preview

    if expected_count != len(rows) or expected_new_total != new_total:
        raise ValidationError(
            "Entries changed since the preview. Run the preview again and confirm its numbers."
        )

    now = timezone.now()
    for old in old_rates:
        old.cancelled_at, old.cancelled_by = now, user
        old.cancel_reason = f"Rate corrected: {reason}"
        old.save(update_fields=["cancelled_at", "cancelled_by", "cancel_reason"])

    def new_row(rate_value, start, end):
        return SupplierMilkRate.objects.create(
            business_id=business_id, supplier=supplier, milk_type=milk_type,
            rate=rate_value, effective_from=start, effective_to=end, created_by=user,
        )

    # the parts of a cancelled period that are outside the corrected window keep their rate
    for old in old_rates:
        if old.effective_from < effective_from:
            new_row(old.rate, old.effective_from, effective_from)
        if old.effective_to is None or old.effective_to > effective_to:
            new_row(old.rate, effective_to, old.effective_to)
    new_row(rate, effective_from, effective_to)

    corrected = []
    for c in entries:
        replacement = correct_milk_collection(
            business_id=business_id, collection_id=c.pk, user=user,
            reason=f"Rate corrected to {rate}: {reason}",
            litres=c.litres, fat=None if c.milk_type == MilkCollection.MilkType.COW else c.fat,
            reprice=True,
        )
        corrected.append({
            "cancelled_collection_id": str(c.pk),
            "cancelled_number": c.collection_number,
            "new_collection_id": str(replacement.pk),
            "new_number": replacement.collection_number,
            "new_amount": replacement.amount,
        })
    return {**preview, "confirmed": True, "corrected": corrected}
