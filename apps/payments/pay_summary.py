"""
"Pay for period" helper: what to pay a supplier now, with the breakdown.

Read-only. It never posts anything; POST .../payments/ stays the only
writer and re-checks every cap against live data (an overpayment simply
becomes supplier advance), so a stale suggestion cannot corrupt balances.
"""
from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db.models import F, Sum
from django.utils import timezone

from apps.businesses.models import Business
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentAllocationReversal,
)
from apps.payments.selectors import get_payment_amounts, get_supplier_position
from apps.payments.statements import _day_start
from apps.purchases.models import PurchaseAdjustment, SupplierPurchase

ZERO = Decimal("0.00")
DEFAULT_DAYS = 5
MAX_DAYS = 366


def _parse_date(value, field):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Use YYYY-MM-DD format."})


def _resolve_period(tz, days, start_date, end_date):
    today = timezone.now().astimezone(tz).date()

    if days is not None and (start_date or end_date):
        raise ValidationError(
            "Use either days or start_date/end_date, not both."
        )

    if start_date or end_date:
        if not (start_date and end_date):
            raise ValidationError(
                "Give both start_date and end_date."
            )
        start = _parse_date(start_date, "start_date")
        end = _parse_date(end_date, "end_date")
    else:
        try:
            days = int(DEFAULT_DAYS if days is None else days)
        except (TypeError, ValueError):
            raise ValidationError({"days": "Enter a whole number of days."})

        if not 1 <= days <= MAX_DAYS:
            raise ValidationError(
                {"days": f"Days must be between 1 and {MAX_DAYS}."}
            )
        # "Last N days" includes today.
        end = today
        start = today - timedelta(days=days - 1)

    if start > end:
        raise ValidationError(
            {"end_date": "End date must be on or after start date."}
        )

    if (end - start).days + 1 > MAX_DAYS:
        raise ValidationError(
            f"The period can be at most {MAX_DAYS} days."
        )

    return start, end


def _grouped(queryset, key):
    return {
        row["k"]: row["t"]
        for row in queryset.values(k=F(key)).annotate(t=Sum("amount"))
    }


def get_pay_summary(
    *, business_id, supplier_id, days=None, start_date=None, end_date=None
):
    tz = ZoneInfo(Business.objects.get(pk=business_id).timezone)
    start, end = _resolve_period(tz, days, start_date, end_date)
    start_at = _day_start(tz, start)
    end_at = _day_start(tz, end + timedelta(days=1))  # exclusive

    position = get_supplier_position(
        business_id=business_id, supplier_id=supplier_id
    )

    purchases = list(
        SupplierPurchase.objects.filter(
            business_id=business_id,
            supplier_id=supplier_id,
            status=SupplierPurchase.Status.RECEIVED,
            is_deleted=False,
            purchased_at__gte=start_at,
            purchased_at__lt=end_at,
        ).order_by("purchased_at", "pk")
    )

    allocs = SupplierPaymentAllocation.objects.filter(
        business_id=business_id, purchase__in=[p.pk for p in purchases]
    )
    allocated = _grouped(allocs, "purchase_id")
    unwound = _grouped(
        SupplierPaymentAllocationReversal.objects.filter(
            allocation__in=allocs
        ),
        "allocation__purchase_id",
    )
    adjusted = _grouped(
        PurchaseAdjustment.objects.filter(
            business_id=business_id,
            purchase__in=[p.pk for p in purchases],
        ),
        "purchase_id",
    )

    rows = []
    period_outstanding = ZERO

    for purchase in purchases:
        net = purchase.total_amount + adjusted.get(purchase.pk, ZERO)
        paid = allocated.get(purchase.pk, ZERO) - unwound.get(purchase.pk, ZERO)
        outstanding = net - paid
        period_outstanding += outstanding
        rows.append({
            "purchase_id": str(purchase.pk),
            "purchase_number": purchase.purchase_number,
            "purchased_at": purchase.purchased_at,
            "net_amount": net,
            "paid": paid,
            "outstanding": outstanding,
        })

    # Identity by construction: older + in-period + later == outstanding.
    # Buckets outside the period are not listed; only their total is.
    other_outstanding = position["outstanding_amount"] - period_outstanding

    payments = []
    for payment in SupplierPayment.objects.filter(
        business_id=business_id,
        supplier_id=supplier_id,
        paid_at__gte=start_at,
        paid_at__lt=end_at,
    ).order_by("paid_at", "pk"):
        amounts = get_payment_amounts(payment)
        payments.append({
            "payment_id": str(payment.pk),
            "payment_number": payment.payment_number,
            "paid_at": payment.paid_at,
            "amount": amounts["amount"],
            "reversed": amounts["reversed"],
            "net": amounts["net"],
            "allocated": amounts["allocated"],
            "unallocated": amounts["unallocated"],
        })

    credit = position["available_credit"]
    due = position["outstanding_amount"]

    return {
        "supplier_id": str(supplier_id),
        "generated_at": timezone.now(),
        "period": {
            "start_date": start,
            "end_date": end,
            "days": (end - start).days + 1,
        },
        "outstanding_amount": due,
        "available_credit": credit,
        # What to pay now: everything owed, less advance already held.
        # Never negative; a negative outstanding is reported as refund_due.
        "suggested_amount": max(due - credit, ZERO),
        "refund_due": max(-due, ZERO),
        "breakdown": {
            "period_outstanding": period_outstanding,
            "outstanding_outside_period": other_outstanding,
            "less_available_credit": credit,
        },
        "period_purchases": rows,
        "period_payments": payments,
    }
