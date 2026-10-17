"""
Customer payments: record, allocate and reverse.

Mirrors apps/payments/services (the supplier side). Differences: the payment
number is generated here, paid_at is optional, and allocations may be given
explicitly at creation time. Every writer takes the customer lock first
(see apps/billing/locks.py), then bills, then the payment.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.billing import selectors
from apps.billing.locks import lock_customer
from apps.billing.models import (
    CustomerBill,
    CustomerPayment,
    CustomerPaymentAllocation,
    CustomerPaymentAllocationReversal,
    CustomerPaymentReversal,
)
from apps.billing.money import to_money
from apps.businesses.models import Business
from apps.businesses.services import require_business_membership
from apps.payments.services import ClientRefConflict

ALLOWED_ROLES = ["OWNER", "MANAGER"]
CLOCK_SKEW = timedelta(minutes=5)
ZERO = Decimal("0.00")
FINALIZED = CustomerBill.Status.FINALIZED


def next_payment_number(business, customer, paid_at):
    """
    PAY-<CUSTOMER_CODE>-<YYMMDD>-<seq>, the date being the payment's local
    day. Counts every payment ever written for the prefix, so a number is
    never reused. Callers hold the customer lock; the unique constraint
    uniq_customer_payment_number is the final guard.
    """
    prefix = "PAY-{}-{:%y%m%d}-".format(
        customer.customer_code,
        paid_at.astimezone(ZoneInfo(business.timezone)),
    )
    used = [
        int(number[len(prefix):])
        for number in CustomerPayment.objects.filter(
            business_id=business.pk,
            customer=customer,
            payment_number__startswith=prefix,
        ).values_list("payment_number", flat=True)
        if number[len(prefix):].isdigit()
    ]
    return f"{prefix}{max(used, default=0) + 1}"


def _clean_allocations(allocations, amount):
    """Validate an explicit [{bill_id, amount}] list; returns [(UUID, Decimal)]."""
    cleaned, seen, total = [], set(), ZERO

    for item in allocations:
        try:
            bill_id = UUID(str(item.get("bill_id")))
        except (AttributeError, TypeError, ValueError):
            raise ValidationError({
                "allocations": "Each allocation needs a valid bill_id."
            })

        if bill_id in seen:
            raise ValidationError({
                "allocations": "A bill can appear only once per payment."
            })

        seen.add(bill_id)
        share = to_money(item.get("amount"), "allocations")
        total += share
        cleaned.append((bill_id, share))

    if total > amount:
        raise ValidationError({
            "allocations": "Allocated amount cannot exceed the payment amount."
        })

    return cleaned


def _replay_or_conflict(
    payment,
    *,
    customer_id,
    amount,
    payment_method,
    reference_number,
    notes,
    paid_at,
    allocations,
):
    """
    Same client_ref: an identical request replays, anything else conflicts.

    paid_at is None when the retry did not send one (the stored time was the
    server's "now" and must not make a retry look different).
    """
    same = (
        str(payment.customer_id) == str(customer_id)
        and payment.amount == amount
        and payment.payment_method == payment_method
        and payment.reference_number == (reference_number or "")
        and payment.notes == (notes or "")
        and (paid_at is None or payment.paid_at == paid_at)
    )

    # ponytail: an omitted allocations list (auto) skips the comparison, as on
    # the supplier side: the plan depended on balances at the first call.
    if same and allocations is not None:
        stored = {
            (UUID(str(a.bill_id)), a.amount)
            for a in payment.allocations.all()
        }
        same = stored == set(allocations)

    if not same:
        raise ClientRefConflict({
            "client_ref": (
                "This reference was already used for a different "
                "payment. Use a new reference for a new payment."
            )
        })

    payment.replayed = True
    return payment


@transaction.atomic
def record_customer_payment(
    *,
    business_id,
    customer_id,
    amount,
    user,
    paid_at=None,
    payment_method=CustomerPayment.PaymentMethod.CASH,
    reference_number="",
    notes="",
    allocations=None,
    client_ref=None,
):
    """
    Post a customer payment. Returns it; `.replayed` is True when client_ref
    matched an identical earlier request (nothing new written) and
    `.allocation_rows` lists the allocations created now.

    allocations: None = apply to the oldest unpaid bills first; a list of
    {"bill_id", "amount"} = exactly those (an empty list keeps everything as
    advance). What is not allocated stays as the customer's advance.
    """
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=ALLOWED_ROLES,
    )

    client_ref = (client_ref or "").strip() or None
    amount = to_money(amount, "amount")

    if payment_method not in CustomerPayment.PaymentMethod.values:
        raise ValidationError({"payment_method": "Invalid payment method."})

    paid_at_given = paid_at is not None
    if paid_at_given and (
        not isinstance(paid_at, datetime) or not timezone.is_aware(paid_at)
    ):
        raise ValidationError({
            "paid_at": "Payment date and time must include a timezone."
        })

    cleaned = (
        None if allocations is None else _clean_allocations(allocations, amount)
    )

    # Lock order: customer -> bills -> payment.
    customer = lock_customer(business_id, customer_id)

    if client_ref:
        replay = CustomerPayment.objects.filter(
            business_id=business_id, client_ref=client_ref
        ).first()

        if replay is not None:
            return _replay_or_conflict(
                replay,
                customer_id=customer_id,
                amount=amount,
                payment_method=payment_method,
                reference_number=reference_number,
                notes=notes,
                paid_at=paid_at if paid_at_given else None,
                allocations=cleaned,
            )

    if customer is None:
        raise ValidationError({
            "customer": (
                "The customer does not exist or belongs to another business."
            )
        })

    now = timezone.now()
    if not paid_at_given:
        paid_at = now
    elif paid_at > now + CLOCK_SKEW:
        raise ValidationError({
            "paid_at": "A payment cannot be dated in the future."
        })

    bills = selectors.lock_finalized_bills(business_id, customer.pk)
    plan = []

    if cleaned is None:
        remaining = amount
        for bill in bills:
            if remaining <= 0:
                break
            if bill.due_amount <= 0:
                continue
            share = min(remaining, bill.due_amount)
            plan.append((bill, share))
            remaining -= share
    else:
        by_id = {bill.pk: bill for bill in bills}
        for bill_id, share in cleaned:
            bill = by_id.get(bill_id)
            if bill is None:
                raise ValidationError({
                    "allocations": (
                        "Each bill must be a finalized bill of this customer."
                    )
                })
            if share > bill.due_amount:
                raise ValidationError({
                    "allocations": (
                        "Allocation exceeds the outstanding amount for "
                        f"bill {bill.bill_number}."
                    )
                })
            plan.append((bill, share))

    business = Business.objects.get(pk=business_id)
    payment_number = next_payment_number(business, customer, paid_at)

    try:
        # Savepoint: a unique-constraint race becomes a 409, not a 500.
        with transaction.atomic():
            payment = CustomerPayment.objects.create(
                business_id=business_id,
                customer=customer,
                payment_number=payment_number,
                paid_at=paid_at,
                amount=amount,
                payment_method=payment_method,
                reference_number=reference_number or "",
                notes=notes or "",
                client_ref=client_ref,
                created_by=user,
            )
    except IntegrityError:
        raise ClientRefConflict({
            "client_ref": (
                "This reference or payment number was just used by "
                "another request."
            )
        })

    payment.allocation_rows = [
        CustomerPaymentAllocation.objects.create(
            business_id=business_id,
            payment=payment,
            bill_id=bill.pk,
            amount=share,
        )
        for bill, share in plan
    ]
    payment.replayed = False
    return payment


def _peek_payment(payment_id, business_id):
    """Tenant-scoped, unlocked read of a payment's customer (no leak)."""
    try:
        return CustomerPayment.objects.only("customer_id").get(
            pk=payment_id, business_id=business_id
        )
    except (CustomerPayment.DoesNotExist, ValidationError, ValueError):
        raise ValidationError("Payment not found.")


@transaction.atomic
def allocate_customer_advance(*, business_id, payment_id, bill_id, amount, user):
    """
    Apply part of a payment's unallocated advance to a finalized bill.

    Returns the allocation; `.replayed` is True when this payment is already
    allocated to this bill for exactly this amount (a retry).
    Lock order: customer -> bill -> payment.
    """
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=ALLOWED_ROLES,
    )
    amount = to_money(amount, "amount")

    ref = _peek_payment(payment_id, business_id)
    lock_customer(business_id, ref.customer_id)

    try:
        bill = (
            CustomerBill.objects.select_for_update()
            .filter(pk=bill_id, business_id=business_id)
            .first()
        )
    except (ValidationError, ValueError):
        bill = None

    if bill is None:
        raise ValidationError("Bill not found for this payment's business.")

    payment = CustomerPayment.objects.select_for_update().get(
        pk=payment_id, business_id=business_id
    )

    if payment.customer_id != bill.customer_id:
        raise ValidationError(
            "Payment and bill must belong to the same customer."
        )

    if bill.status != FINALIZED:
        raise ValidationError("Only finalized bills can receive allocations.")

    existing = CustomerPaymentAllocation.objects.filter(
        payment=payment, bill=bill
    ).first()

    if existing is not None:
        if existing.amount == amount and not existing.unwinds.exists():
            existing.replayed = True
            return existing

        raise ValidationError(
            "This payment is already allocated to this bill."
        )

    amounts = selectors.get_payment_amounts(payment)

    if amounts["net"] <= 0:
        raise ValidationError("A reversed payment cannot be allocated.")

    if amount > amounts["unallocated"]:
        raise ValidationError("Allocation exceeds the remaining advance.")

    outstanding = bill.total_amount - selectors.allocated_to_bill(
        business_id, bill
    )

    if amount > outstanding:
        raise ValidationError(
            "Allocation exceeds the bill's outstanding amount."
        )

    allocation = CustomerPaymentAllocation.objects.create(
        business_id=business_id,
        payment=payment,
        bill=bill,
        amount=amount,
    )
    allocation.replayed = False
    return allocation


def apply_credit_to_bill(*, bill):
    """
    Use the customer's held advance on a bill, oldest payment first.

    The caller holds the customer lock and the bill's row lock and has
    already marked the bill FINALIZED (lock order: bill -> payment).
    Returns the allocations created.
    """
    due = bill.total_amount - selectors.allocated_to_bill(
        bill.business_id, bill
    )
    created = []

    for payment in selectors.lock_credit_payments(
        bill.business_id, bill.customer_id
    ):
        if due <= 0:
            break

        available = selectors.get_payment_amounts(payment)["unallocated"]
        if available <= 0:
            continue

        share = min(due, available)
        created.append(
            CustomerPaymentAllocation.objects.create(
                business_id=bill.business_id,
                payment=payment,
                bill=bill,
                amount=share,
            )
        )
        due -= share

    return created


def _reversal_replay_or_conflict(reversal, *, payment_id, amount, reason):
    same = (
        str(reversal.payment_id) == str(payment_id)
        and reversal.reason == reason
        and (amount is None or reversal.amount == amount)
    )

    if not same:
        raise ClientRefConflict({
            "client_ref": (
                "This reference was already used for a different "
                "reversal. Use a new reference for a new reversal."
            )
        })

    reversal.replayed = True
    return reversal


def _unwound_rows(reversal):
    return list(
        CustomerPaymentAllocationReversal.objects.filter(reversal=reversal)
        .select_related("allocation__bill")
        .order_by("created_at", "pk")
    )


@transaction.atomic
def reverse_customer_payment(
    *,
    business_id,
    payment_id,
    reason,
    user,
    amount=None,
    client_ref=None,
):
    """
    Reverse all (amount=None) or part of what is left of a payment.

    The reversed amount comes out of the unallocated advance first; only the
    rest unwinds allocations on finalized bills, newest first, each recorded
    as an explicit CustomerPaymentAllocationReversal row. Nothing is edited.
    Returns the reversal; `.replayed` is True for an identical client_ref
    retry and `.unwound` lists the (allocation, amount) rows taken back.

    Lock order: customer -> payment. Unwinding needs no bill lock: it only
    frees bill capacity, and a bill voided meanwhile just makes that
    allocation irrelevant.
    """
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=ALLOWED_ROLES,
    )

    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({
            "reason": "A reason is required to reverse a payment."
        })

    if amount is not None:
        amount = to_money(amount, "amount")

    client_ref = (client_ref or "").strip() or None

    ref = _peek_payment(payment_id, business_id)
    lock_customer(business_id, ref.customer_id)
    payment = CustomerPayment.objects.select_for_update().get(
        pk=payment_id, business_id=business_id
    )

    if client_ref:
        replay = CustomerPaymentReversal.objects.filter(
            business_id=business_id, client_ref=client_ref
        ).first()

        if replay is not None:
            replay = _reversal_replay_or_conflict(
                replay, payment_id=payment.pk, amount=amount, reason=reason
            )
            replay.unwound = _unwound_rows(replay)
            return replay

    amounts = selectors.get_payment_amounts(payment)

    if amounts["net"] <= 0:
        raise ValidationError("This payment has already been fully reversed.")

    if amount is None:
        amount = amounts["net"]

    if amount > amounts["net"]:
        raise ValidationError(
            "Cannot reverse more than the remaining payment amount "
            f"({amounts['net']})."
        )

    from_advance = min(amount, max(amounts["unallocated"], ZERO))
    to_unwind = amount - from_advance

    try:
        # Savepoint: a unique-constraint race becomes a 409, not a 500.
        with transaction.atomic():
            reversal = CustomerPaymentReversal.objects.create(
                business_id=business_id,
                payment=payment,
                amount=amount,
                reason=reason,
                # Never before the payment itself.
                reversed_at=max(timezone.now(), payment.paid_at),
                client_ref=client_ref,
                created_by=user,
            )
    except IntegrityError:
        raise ClientRefConflict({
            "client_ref": "This reference was just used by another request."
        })

    if to_unwind > 0:
        allocations = CustomerPaymentAllocation.objects.filter(
            payment=payment, bill__status=FINALIZED
        ).order_by("-created_at", "-pk")

        for allocation in allocations:
            if to_unwind <= 0:
                break

            effective = allocation.amount - (
                allocation.unwinds.aggregate(t=Sum("amount"))["t"] or ZERO
            )

            if effective <= 0:
                continue

            take = min(effective, to_unwind)
            CustomerPaymentAllocationReversal.objects.create(
                business_id=business_id,
                reversal=reversal,
                allocation=allocation,
                amount=take,
            )
            to_unwind -= take

        if to_unwind > 0:  # unreachable while the invariants hold
            raise ValidationError(
                "Payment allocations are inconsistent; nothing was reversed."
            )

    reversal.replayed = False
    reversal.unwound = _unwound_rows(reversal)
    return reversal
