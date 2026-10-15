from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q, Sum
from django.utils import timezone

from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)
from apps.purchases.models import PurchaseAdjustment, SupplierPurchase


ZERO = Decimal("0.00")


def _sum_amount(queryset, field="amount"):
    return queryset.aggregate(total=Sum(field))["total"] or ZERO


def _date_filter(queryset, field, start_date=None, end_date=None):
    if start_date is not None:
        queryset = queryset.filter(
            **{f"{field}__date__gte": start_date}
        )

    if end_date is not None:
        queryset = queryset.filter(
            **{f"{field}__date__lte": end_date}
        )

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
            start_date = date.fromisoformat(start_date)
        except ValueError:
            raise ValidationError(
                {"start_date": "Use YYYY-MM-DD format."}
            )

    if isinstance(end_date, str):
        try:
            end_date = date.fromisoformat(end_date)
        except ValueError:
            raise ValidationError(
                {"end_date": "Use YYYY-MM-DD format."}
            )

    if start_date and end_date and start_date > end_date:
        raise ValidationError(
            {"end_date": "End date must be on or after start date."}
        )

    # Default to today for a statement's closing date.
    end_date = end_date or timezone.localdate()

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
        opening_purchases = purchases.filter(
            purchased_at__date__lt=start_date
        )
        opening_payments = payments.filter(
            paid_at__date__lt=start_date
        )
        opening_reversals = reversals.filter(
            reversed_at__date__lt=start_date
        )

        opening_balance = (
            _sum_amount(opening_purchases, "total_amount")
            + _sum_amount(
                adjustments.filter(occurred_at__date__lt=start_date)
            )
            - _sum_amount(opening_payments)
            + _sum_amount(opening_reversals)
        )
    else:
        opening_balance = ZERO

    # Transactions displayed in the selected period.
    period_purchases = _date_filter(
        purchases, "purchased_at", start_date, end_date
    )
    period_adjustments = _date_filter(
        adjustments, "occurred_at", start_date, end_date
    ).select_related("purchase")
    period_payments = _date_filter(
        payments, "paid_at", start_date, end_date
    )
    period_reversals = _date_filter(
        reversals, "reversed_at", start_date, end_date
    )

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

    # Calculate the supplier's position as of the statement end date.
    purchases_to_date = purchases.filter(
        purchased_at__date__lte=end_date
    )
    payments_to_date = payments.filter(
        paid_at__date__lte=end_date
    ).filter(
        Q(reversal__isnull=True)
        | Q(reversal__reversed_at__date__gt=end_date)
    )


    allocations_to_date = SupplierPaymentAllocation.objects.filter(
        business_id=business_id,
        payment__business_id=business_id,
        payment__supplier_id=supplier_id,
        payment__paid_at__date__lte=end_date,
        payment__created_at__date__lte=end_date,
        purchase__business_id=business_id,
        purchase__supplier_id=supplier_id,
        purchase__status=SupplierPurchase.Status.RECEIVED,
        purchase__purchased_at__date__lte=end_date,
        created_at__date__lte=end_date,
    ).filter(
        Q(payment__reversal__isnull=True)
        | Q(payment__reversal__reversed_at__date__gt=end_date)
    )


    total_purchases = _sum_amount(
        purchases_to_date, "total_amount"
    ) + _sum_amount(
        adjustments.filter(
            occurred_at__date__lte=end_date,
            purchase__purchased_at__date__lte=end_date,
        )
    )
    total_valid_payments = _sum_amount(payments_to_date)
    total_allocated = _sum_amount(allocations_to_date)

    outstanding_amount = max(
        total_purchases - total_allocated,
        ZERO,
    )

    available_credit = max(
        total_valid_payments - total_allocated,
        ZERO,
    )

    return {
        "business_id": str(business_id),
        "supplier_id": str(supplier_id),
        "start_date": start_date,
        "end_date": end_date,
        "opening_balance": opening_balance,
        "transactions": events,
        "closing_balance": running_balance,
        "total_purchases": total_purchases,
        "total_payments": total_valid_payments,
        "allocated_payments": total_allocated,
        "outstanding_amount": outstanding_amount,
        "available_credit": available_credit,
    }