"""
Bulk customer / supplier money, one set of grouped queries for every party.

These mirror get_customer_position / get_supplier_position exactly (same
filters, same as_of rules); tests/reports/test_positions.py asserts equality
for every party, so the per-party selectors stay the single source of truth.
"""
from django.db.models import Q, Sum

from apps.billing.models import (
    CustomerBill,
    CustomerPayment,
    CustomerPaymentAllocation,
    CustomerPaymentAllocationReversal,
    CustomerPaymentReversal,
)
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentAllocationReversal,
    SupplierPaymentReversal,
)
from apps.purchases.models import PurchaseAdjustment, SupplierPurchase
from apps.reports.common import ZERO


def _grouped(queryset, key, field="amount"):
    rows = queryset.order_by().values(key).annotate(t=Sum(field))
    return {r[key]: r["t"] or ZERO for r in rows}


def customer_positions(business_id, as_of=None):
    """{customer_id: get_customer_position-shaped dict} for customers with money rows."""
    bills = CustomerBill.objects.filter(
        business_id=business_id, finalized_at__isnull=False
    )
    if as_of is None:
        bills = bills.filter(status=CustomerBill.Status.FINALIZED)
    else:
        bills = bills.filter(finalized_at__lt=as_of).filter(
            Q(voided_at__isnull=True) | Q(voided_at__gte=as_of)
        )

    payments = CustomerPayment.objects.filter(business_id=business_id)
    reversals = CustomerPaymentReversal.objects.filter(payment__in=payments)
    if as_of:
        payments = payments.filter(paid_at__lt=as_of)
        reversals = CustomerPaymentReversal.objects.filter(
            payment__in=payments, reversed_at__lt=as_of
        )
    allocations = CustomerPaymentAllocation.objects.filter(
        business_id=business_id, payment__in=payments, bill__in=bills
    )
    unwinds = CustomerPaymentAllocationReversal.objects.filter(
        allocation__in=allocations
    )
    if as_of:
        unwinds = unwinds.filter(reversal__reversed_at__lt=as_of)

    billed = _grouped(bills, "customer_id", "total_amount")
    paid = _grouped(payments, "customer_id")
    reversed_ = _grouped(reversals, "payment__customer_id")
    allocated = _grouped(allocations, "payment__customer_id")
    unwound = _grouped(unwinds, "allocation__payment__customer_id")

    out = {}
    for cid in set(billed) | set(paid):
        total_billed = billed.get(cid, ZERO)
        total_payments = paid.get(cid, ZERO) - reversed_.get(cid, ZERO)
        alloc = allocated.get(cid, ZERO) - unwound.get(cid, ZERO)
        out[cid] = {
            "total_billed": total_billed,
            "total_payments": total_payments,
            "allocated_payments": alloc,
            "outstanding_amount": total_billed - alloc,
            "available_credit": max(total_payments - alloc, ZERO),
            "net_balance": total_billed - total_payments,
        }
    return out


def supplier_positions(business_id, as_of=None):
    """{supplier_id: get_supplier_position-shaped dict} for suppliers with money rows."""
    purchases = SupplierPurchase.objects.filter(
        business_id=business_id,
        status=SupplierPurchase.Status.RECEIVED,
        is_deleted=False,
    )
    adjustments = PurchaseAdjustment.objects.filter(business_id=business_id)
    payments = SupplierPayment.objects.filter(business_id=business_id)
    if as_of:
        purchases = purchases.filter(purchased_at__lt=as_of)
        adjustments = adjustments.filter(occurred_at__lt=as_of)
        payments = payments.filter(paid_at__lt=as_of)
    adjustments = adjustments.filter(purchase__in=purchases)
    reversals = SupplierPaymentReversal.objects.filter(payment__in=payments)
    if as_of:
        reversals = reversals.filter(reversed_at__lt=as_of)
    allocations = SupplierPaymentAllocation.objects.filter(
        business_id=business_id, payment__in=payments, purchase__in=purchases
    )
    unwinds = SupplierPaymentAllocationReversal.objects.filter(
        allocation__in=allocations
    )
    if as_of:
        unwinds = unwinds.filter(reversal__reversed_at__lt=as_of)

    bought = _grouped(purchases, "supplier_id", "total_amount")
    adjusted = _grouped(adjustments, "purchase__supplier_id")
    paid = _grouped(payments, "supplier_id")
    reversed_ = _grouped(reversals, "payment__supplier_id")
    allocated = _grouped(allocations, "payment__supplier_id")
    unwound = _grouped(unwinds, "allocation__payment__supplier_id")

    out = {}
    for sid in set(bought) | set(paid) | set(adjusted):
        total_purchases = bought.get(sid, ZERO) + adjusted.get(sid, ZERO)
        total_payments = paid.get(sid, ZERO) - reversed_.get(sid, ZERO)
        alloc = allocated.get(sid, ZERO) - unwound.get(sid, ZERO)
        out[sid] = {
            "total_purchases": total_purchases,
            "total_payments": total_payments,
            "allocated_payments": alloc,
            "outstanding_amount": total_purchases - alloc,
            "available_credit": max(total_payments - alloc, ZERO),
        }
    return out
