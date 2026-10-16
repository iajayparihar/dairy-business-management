from decimal import Decimal

from django.db.models import Q, Sum

from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentAllocationReversal,
    SupplierPaymentReversal,
)
from apps.purchases.models import PurchaseAdjustment, SupplierPurchase

ZERO = Decimal("0.00")


def _sum(queryset, field="amount"):
    return queryset.aggregate(total=Sum(field))["total"] or ZERO


def effective_allocated(allocations, as_of=None):
    """
    Allocated amount net of unwinds (partial/full payment reversals).
    allocations: a SupplierPaymentAllocation queryset.
    """
    unwinds = SupplierPaymentAllocationReversal.objects.filter(
        allocation__in=allocations
    )

    if as_of:
        unwinds = unwinds.filter(reversal__reversed_at__lt=as_of)

    return _sum(allocations) - _sum(unwinds)


def allocated_to_purchase(business_id, purchase):
    """What payments currently cover of a purchase (after reversals)."""
    return effective_allocated(
        SupplierPaymentAllocation.objects.filter(
            business_id=business_id, purchase=purchase
        )
    )


def get_payment_amounts(payment):
    """
    amount          posted (never changes)
    reversed        sum of reversals
    net             amount - reversed (what the payment is still worth)
    allocated       currently applied to RECEIVED purchases
    unallocated     net - allocated = remaining supplier advance

    Allocations on cancelled purchases are ignored: that money has
    returned to the supplier's credit (see ARCHITECTURE / CLAUDE.md).
    Callers that decide anything hold the payment row lock.
    """
    reversed_total = _sum(
        SupplierPaymentReversal.objects.filter(payment=payment)
    )
    allocated = effective_allocated(
        SupplierPaymentAllocation.objects.filter(
            payment=payment,
            purchase__status=SupplierPurchase.Status.RECEIVED,
        )
    )
    net = payment.amount - reversed_total

    return {
        "amount": payment.amount,
        "reversed": reversed_total,
        "net": net,
        "allocated": allocated,
        "unallocated": net - allocated,
    }


def get_supplier_position(*, business_id, supplier_id, as_of=None):
    """
    The one place supplier money is added up (balance and statement both
    use it).

    as_of: aware datetime, exclusive upper bound on business time
    (purchased_at / occurred_at / paid_at / reversed_at); None = now.
    Reversals made before as_of reduce the payment (and unwind allocations,
    via explicit rows). An allocation counts from max(paid_at,
    purchased_at) - the only business-time notion it has.

    outstanding_amount = purchases + adjustments - allocated; NOT clamped
    (negative = refund due) and NOT netted against advance, which is
    reported separately as available_credit.
    """
    def before(field):
        return Q(**{f"{field}__lt": as_of}) if as_of else Q()

    purchases = SupplierPurchase.objects.filter(
        before("purchased_at"),
        business_id=business_id,
        supplier_id=supplier_id,
        status=SupplierPurchase.Status.RECEIVED,
        is_deleted=False,
    )

    adjustments = PurchaseAdjustment.objects.filter(
        before("occurred_at"),
        business_id=business_id,
        purchase__in=purchases,
    )

    payments = SupplierPayment.objects.filter(
        before("paid_at"),
        business_id=business_id,
        supplier_id=supplier_id,
    )

    reversals = SupplierPaymentReversal.objects.filter(payment__in=payments)

    if as_of:
        reversals = reversals.filter(reversed_at__lt=as_of)

    allocations = SupplierPaymentAllocation.objects.filter(
        business_id=business_id,
        payment__in=payments,
        purchase__in=purchases,
    )

    total_purchases = _sum(purchases, "total_amount") + _sum(adjustments)
    total_payments = _sum(payments) - _sum(reversals)
    allocated = effective_allocated(allocations, as_of)

    return {
        "total_purchases": total_purchases,
        "total_payments": total_payments,
        "allocated_payments": allocated,
        "outstanding_amount": total_purchases - allocated,
        "available_credit": max(total_payments - allocated, ZERO),
    }


def get_supplier_balance(*, business_id, supplier_id):
    position = get_supplier_position(
        business_id=business_id, supplier_id=supplier_id
    )
    position.pop("total_payments")
    return position
