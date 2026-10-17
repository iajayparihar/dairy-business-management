"""
Read-side rules for customer money. One arithmetic for balance and statement.

Sign: positive = the customer owes us, negative = the customer has advance.
A FINALIZED bill is the only charge. Allocations on a VOID bill are ignored,
which returns that money to the customer's credit.
"""
from decimal import Decimal

from django.db.models import (
    DecimalField,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce

from apps.billing.models import (
    CustomerBill,
    CustomerPayment,
    CustomerPaymentAllocation,
    CustomerPaymentAllocationReversal,
    CustomerPaymentReversal,
)

ZERO = Decimal("0.00")
MONEY = DecimalField(max_digits=14, decimal_places=2)
FINALIZED = CustomerBill.Status.FINALIZED

# Allocation order: oldest period first.
BILL_ORDER = ("period_start", "period_end", "finalized_at", "pk")
PAYMENT_ORDER = ("paid_at", "created_at", "pk")


def _sum(queryset, field="amount"):
    return queryset.aggregate(total=Sum(field))["total"] or ZERO


def _total_per_outer_row(queryset, group_field):
    """Sum of amount for the outer row, 0 when it has none."""
    return Coalesce(
        Subquery(
            queryset.order_by()
            .values(group_field)
            .annotate(total=Sum("amount"))
            .values("total"),
            output_field=MONEY,
        ),
        Value(ZERO),
        output_field=MONEY,
    )


def effective_allocated(allocations, as_of=None):
    """
    Allocated amount net of unwinds (payment reversals).
    allocations: a CustomerPaymentAllocation queryset.
    """
    unwinds = CustomerPaymentAllocationReversal.objects.filter(
        allocation__in=allocations
    )

    if as_of:
        unwinds = unwinds.filter(reversal__reversed_at__lt=as_of)

    return _sum(allocations) - _sum(unwinds)


def allocated_to_bill(business_id, bill):
    """What payments currently cover of a bill (after reversals)."""
    return effective_allocated(
        CustomerPaymentAllocation.objects.filter(
            business_id=business_id, bill=bill
        )
    )


def with_paid_amount(bills):
    """
    Annotate bills with paid_amount (allocations net of unwinds) and
    due_amount. Only meaningful for FINALIZED bills.
    """
    allocated = _total_per_outer_row(
        CustomerPaymentAllocation.objects.filter(bill=OuterRef("pk")), "bill"
    )
    unwound = _total_per_outer_row(
        CustomerPaymentAllocationReversal.objects.filter(
            allocation__bill=OuterRef("pk")
        ),
        "allocation__bill",
    )
    return bills.annotate(paid_amount=allocated - unwound).annotate(
        due_amount=F("total_amount") - F("paid_amount")
    )


def with_unallocated(payments):
    """
    Annotate payments with unallocated_amount = what the payment is still
    worth minus what is applied to FINALIZED bills.
    """
    reversed_total = _total_per_outer_row(
        CustomerPaymentReversal.objects.filter(payment=OuterRef("pk")),
        "payment",
    )
    allocated = _total_per_outer_row(
        CustomerPaymentAllocation.objects.filter(
            payment=OuterRef("pk"), bill__status=FINALIZED
        ),
        "payment",
    )
    unwound = _total_per_outer_row(
        CustomerPaymentAllocationReversal.objects.filter(
            allocation__payment=OuterRef("pk"),
            allocation__bill__status=FINALIZED,
        ),
        "allocation__payment",
    )
    return payments.annotate(
        unallocated_amount=F("amount") - reversed_total - allocated + unwound
    )


def lock_finalized_bills(business_id, customer_id):
    """
    Lock the customer's FINALIZED bills (pk order) and return them oldest
    first with paid_amount / due_amount. Caller holds the customer lock.
    """
    ids = list(
        CustomerBill.objects.select_for_update()
        .filter(
            business_id=business_id, customer_id=customer_id, status=FINALIZED
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    return list(
        with_paid_amount(CustomerBill.objects.filter(pk__in=ids)).order_by(
            *BILL_ORDER
        )
    )


def lock_credit_payments(business_id, customer_id):
    """
    Lock the customer's payments that still hold unallocated money and return
    them oldest first. Caller holds the customer lock.
    """
    candidates = list(
        with_unallocated(
            CustomerPayment.objects.filter(
                business_id=business_id, customer_id=customer_id
            )
        )
        .filter(unallocated_amount__gt=0)
        .values_list("pk", flat=True)
    )

    if not candidates:
        return []

    list(
        CustomerPayment.objects.select_for_update()
        .filter(pk__in=candidates)
        .order_by("pk")
    )
    return list(
        CustomerPayment.objects.filter(pk__in=candidates).order_by(
            *PAYMENT_ORDER
        )
    )


def get_payment_amounts(payment):
    """
    amount          posted (never changes)
    reversed        sum of reversals
    net             amount - reversed (what the payment is still worth)
    allocated       currently applied to FINALIZED bills
    unallocated     net - allocated = the customer's advance from it

    Allocations on VOID bills are ignored: that money is back in credit.
    Callers that decide anything hold the payment row lock.
    """
    reversed_total = _sum(
        CustomerPaymentReversal.objects.filter(payment=payment)
    )
    allocated = effective_allocated(
        CustomerPaymentAllocation.objects.filter(
            payment=payment, bill__status=FINALIZED
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


def bills_as_of(business_id, customer_id, as_of=None):
    """
    Bills counted at `as_of` (exclusive upper bound; None = now): finalized
    before it and not yet voided at that moment. A statement re-run for an
    old date therefore stays the same after a later void.
    """
    bills = CustomerBill.objects.filter(
        business_id=business_id,
        customer_id=customer_id,
        finalized_at__isnull=False,
    )

    if as_of is None:
        return bills.filter(status=FINALIZED)

    return bills.filter(finalized_at__lt=as_of).filter(
        Q(voided_at__isnull=True) | Q(voided_at__gte=as_of)
    )


def get_customer_position(*, business_id, customer_id, as_of=None):
    """
    The one place customer money is added up (balance and statement both
    use it).

    as_of: aware datetime, exclusive upper bound on business time
    (finalized_at / voided_at / paid_at / reversed_at); None = now. An
    allocation counts from the later of its payment and its bill.

    outstanding_amount = total_billed - allocated (what is still owed on
    bills); available_credit = total_payments - allocated (the advance);
    net_balance = total_billed - total_payments, so
    net_balance == outstanding_amount - available_credit.
    Positive net_balance = the customer owes; negative = advance.
    """
    bills = bills_as_of(business_id, customer_id, as_of)

    payments = CustomerPayment.objects.filter(
        business_id=business_id, customer_id=customer_id
    )
    if as_of:
        payments = payments.filter(paid_at__lt=as_of)

    reversals = CustomerPaymentReversal.objects.filter(payment__in=payments)
    if as_of:
        reversals = reversals.filter(reversed_at__lt=as_of)

    allocations = CustomerPaymentAllocation.objects.filter(
        business_id=business_id, payment__in=payments, bill__in=bills
    )

    total_billed = _sum(bills, "total_amount")
    total_payments = _sum(payments) - _sum(reversals)
    allocated = effective_allocated(allocations, as_of)

    return {
        "total_billed": total_billed,
        "total_payments": total_payments,
        "allocated_payments": allocated,
        "outstanding_amount": total_billed - allocated,
        "available_credit": max(total_payments - allocated, ZERO),
        "net_balance": total_billed - total_payments,
    }
