from apps.common.dates import parse_iso_date
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone

from apps.businesses.models import Business
from apps.payments.models import SupplierPayment, SupplierPaymentReversal
from apps.payments.selectors import get_supplier_position
from apps.purchases.models import MilkCollection, PurchaseAdjustment, SupplierPurchase
from apps.suppliers.models import Supplier


ZERO = Decimal("0.00")


def _sum_amount(queryset, field="amount"):
    return queryset.aggregate(total=Sum(field))["total"] or ZERO


def _day_start(tz, day):
    """Start of a calendar day in the business timezone."""
    return datetime.combine(day, time.min, tzinfo=tz)


def _range_filter(queryset, field, start, end):
    """Half-open [start, end) on business time; either bound may be None."""
    if start is not None:
        queryset = queryset.filter(**{f"{field}__gte": start})

    if end is not None:
        queryset = queryset.filter(**{f"{field}__lt": end})

    return queryset


def get_supplier_account_statement(
    *,
    business_id,
    supplier_id,
    start_date=None,
    end_date=None,
):
    """
    Return a read-only supplier statement for a business and supplier.

    Positive running balance: business owes the supplier.
    Negative running balance: supplier has a net credit.

    Dates are inclusive. The statement uses the purchase/payment
    timestamps and reversal timestamps for chronological entries.
    """

    if isinstance(start_date, str):
        try:
            start_date = parse_iso_date(start_date)
        except ValueError:
            raise ValidationError(
                {"start_date": "Use YYYY-MM-DD format."}
            )

    if isinstance(end_date, str):
        try:
            end_date = parse_iso_date(end_date)
        except ValueError:
            raise ValidationError(
                {"end_date": "Use YYYY-MM-DD format."}
            )

    if start_date and end_date and start_date > end_date:
        raise ValidationError(
            {"end_date": "End date must be on or after start date."}
        )

    # Day boundaries are in the business timezone, as half-open ranges
    # [start_date 00:00, end_date + 1 day 00:00).
    business = Business.objects.get(pk=business_id)
    supplier = Supplier.objects.get(pk=supplier_id, business_id=business_id)
    tz = ZoneInfo(business.timezone)
    end_date = end_date or timezone.now().astimezone(tz).date()
    start_at = _day_start(tz, start_date) if start_date else None
    end_at = _day_start(tz, end_date + timedelta(days=1))

    purchases = SupplierPurchase.objects.filter(
        business_id=business_id,
        supplier_id=supplier_id,
        status=SupplierPurchase.Status.RECEIVED,
        is_deleted=False,
    )

    # Returns/corrections: signed payable changes, dated when posted.
    adjustments = PurchaseAdjustment.objects.filter(
        business_id=business_id,
        purchase__supplier_id=supplier_id,
        purchase__status=SupplierPurchase.Status.RECEIVED,
        purchase__is_deleted=False,
    )

    payments = SupplierPayment.objects.filter(
        business_id=business_id,
        supplier_id=supplier_id,
    )

    reversals = SupplierPaymentReversal.objects.filter(
        business_id=business_id,
        payment__supplier_id=supplier_id,
    )

    # Opening balance includes all activity before the start date.
    if start_date:
        opening_balance = (
            _sum_amount(
                purchases.filter(purchased_at__lt=start_at), "total_amount"
            )
            + _sum_amount(adjustments.filter(occurred_at__lt=start_at))
            - _sum_amount(payments.filter(paid_at__lt=start_at))
            + _sum_amount(reversals.filter(reversed_at__lt=start_at))
        )
    else:
        opening_balance = ZERO

    # Transactions displayed in the selected period.
    period_purchases = _range_filter(
        purchases, "purchased_at", start_at, end_at
    )
    period_adjustments = _range_filter(
        adjustments, "occurred_at", start_at, end_at
    ).select_related("purchase")
    period_payments = _range_filter(payments, "paid_at", start_at, end_at)
    period_reversals = _range_filter(
        reversals, "reversed_at", start_at, end_at
    ).select_related("payment")

    events = []

    for purchase in period_purchases:
        events.append({
            "timestamp": purchase.purchased_at,
            "priority": 1,
            "type": "PURCHASE",
            "reference": purchase.purchase_number,
            "description": purchase.notes or "Supplier purchase",
            "amount": purchase.total_amount,
            "debit": purchase.total_amount,
            "credit": ZERO,
        })

    for adj in period_adjustments:
        events.append({
            "timestamp": adj.occurred_at,
            "priority": 1,
            "type": f"PURCHASE_{adj.adjustment_type}",
            "reference": adj.purchase.purchase_number,
            "description": adj.reason,
            "amount": abs(adj.amount),
            "debit": max(adj.amount, ZERO),
            "credit": max(-adj.amount, ZERO),
        })

    for payment in period_payments:
        events.append({
            "timestamp": payment.paid_at,
            "priority": 2,
            "type": "PAYMENT",
            "reference": payment.payment_number,
            "description": (
                f"Supplier payment ({payment.get_payment_method_display()})"
            ),
            "amount": payment.amount,
            "debit": ZERO,
            "credit": payment.amount,
        })

    for reversal in period_reversals:
        events.append({
            "timestamp": reversal.reversed_at,
            "priority": 3,
            "type": "PAYMENT_REVERSAL",
            "reference": reversal.payment.payment_number,
            "description": f"Payment reversal: {reversal.reason}",
            "amount": reversal.amount,
            "debit": reversal.amount,
            "credit": ZERO,
        })

    # Cancelled milk entries (wrong can, or the wrong half of a correction) are listed for
    # transparency. They carry no debit or credit, so the balance never moves for them.
    cancelled = list(
        _range_filter(
            MilkCollection.objects.filter(
                business_id=business_id, supplier_id=supplier_id,
                status=MilkCollection.Status.CANCELLED,
            ),
            "collected_at", start_at, end_at,
        )
    )
    replacements = dict(
        MilkCollection.objects.filter(replaces_id__in=[c.pk for c in cancelled])
        .values_list("replaces_id", "collection_number")
    )
    for c in cancelled:
        note = f"Cancelled: {c.cancel_reason}" if c.cancel_reason else "Cancelled"
        if c.pk in replacements:
            note += f" (replaced by MILK-{replacements[c.pk]})"
        events.append({
            "timestamp": c.collected_at,
            "priority": 1,
            "type": "MILK_CANCELLED",
            "reference": f"MILK-{c.collection_number}",  # same form as the mirror purchase
            "description": note,
            "amount": c.amount,
            "debit": ZERO,
            "credit": ZERO,
            "counted": False,
        })

    for event in events:
        event.setdefault("counted", True)

    # Stable ordering for events that share a timestamp.
    events.sort(
        key=lambda item: (
            item["timestamp"],
            item["priority"],
            item["reference"],
        )
    )

    running_balance = opening_balance

    for event in events:
        running_balance += event["debit"] - event["credit"]
        event["balance"] = running_balance

    # Same arithmetic as get_supplier_balance, as of the closing instant.
    position = get_supplier_position(
        business_id=business_id, supplier_id=supplier_id, as_of=end_at
    )

    return {
        "business_id": str(business_id),
        "business_name": business.name,
        "currency": business.currency,
        "timezone": business.timezone,
        "supplier_id": str(supplier_id),
        "supplier_code": supplier.supplier_code,
        "supplier_name": supplier.name,
        "generated_at": timezone.now(),
        "start_date": start_date,
        "end_date": end_date,
        "opening_balance": opening_balance,
        "transactions": events,
        "closing_balance": running_balance,
        **position,
    }