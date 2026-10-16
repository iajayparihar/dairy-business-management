
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.businesses.services import require_business_membership
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentAllocationReversal,
    SupplierPaymentReversal,
)
from apps.payments.selectors import allocated_to_purchase, get_payment_amounts
from apps.purchases.models import SupplierPurchase
from apps.suppliers.models import Supplier

ALLOWED_ROLES = ["OWNER", "MANAGER"]


def _to_decimal(value, field_name):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError({field_name: "Enter a valid amount."})

    if not amount.is_finite() or amount <= 0:
        raise ValidationError({field_name: "Amount must be positive."})

    if amount.as_tuple().exponent < -2:
        raise ValidationError({
            field_name: "Amount can have at most 2 decimal places."
        })

    return amount


class ClientRefConflict(ValidationError):
    """client_ref reused for a different request (API answers 409)."""


def _allocated_to_purchase(business_id, purchase):
    """Allocations net of reversals (the shared outstanding rule)."""
    return allocated_to_purchase(business_id, purchase)


def _plan_auto_allocation(*, business_id, supplier_id, amount):
    """Oldest received purchase first; caller holds the supplier lock."""
    purchases = (
        SupplierPurchase.objects.select_for_update()
        .filter(
            business_id=business_id,
            supplier_id=supplier_id,
            status=SupplierPurchase.Status.RECEIVED,
            is_deleted=False,
        )
        .order_by("purchased_at", "pk")
    )

    plan = []
    remaining = amount

    for purchase in purchases:
        if remaining <= 0:
            break

        outstanding = purchase.net_amount - _allocated_to_purchase(
            business_id, purchase
        )

        if outstanding <= 0:
            continue

        share = min(remaining, outstanding)
        plan.append({"purchase_id": str(purchase.pk), "amount": share})
        remaining -= share

    return plan


def _replay_or_conflict(
    payment,
    *,
    supplier_id,
    payment_number,
    paid_at,
    amount,
    payment_method,
    reference_number,
    notes,
    allocations,
):
    """Same client_ref: identical request replays, anything else conflicts."""
    same = (
        str(payment.supplier_id) == str(supplier_id)
        and payment.payment_number == payment_number
        and payment.paid_at == paid_at
        and payment.amount == amount
        and payment.payment_method == payment_method
        and payment.reference_number == (reference_number or "")
        and payment.notes == (notes or "")
    )

    # ponytail: auto_allocate replays skip the allocation comparison
    # (the plan depends on balances at first call); explicit lists compare.
    if same and allocations is not None:
        stored = {
            (str(a.purchase_id), a.amount)
            for a in payment.allocations.all()
        }
        asked = {
            (str(i.get("purchase_id")), _to_decimal(i.get("amount"), "amount"))
            for i in allocations
        }
        same = stored == asked

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
def record_supplier_payment(
    *,
    business_id,
    supplier_id,
    payment_number,
    paid_at,
    amount,
    user,
    payment_method=SupplierPayment.PaymentMethod.CASH,
    reference_number="",
    notes="",
    allocations=None,
    auto_allocate=False,
    client_ref=None,
):
    """
    Post a supplier payment. Returns the payment; `.replayed` is True when
    client_ref matched an identical earlier request (nothing new created).

    allocations: explicit [{"purchase_id", "amount"}] list.
    auto_allocate: settle the oldest outstanding purchases first; any rest
    stays as supplier advance. The two are mutually exclusive.
    """
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=ALLOWED_ROLES,
    )

    if auto_allocate and allocations:
        raise ValidationError(
            "Use either explicit allocations or auto_allocate, not both."
        )

    client_ref = (client_ref or "").strip() or None
    payment_number = (payment_number or "").strip()

    if not payment_number:
        raise ValidationError({"payment_number": "Payment number is required."})

    if not timezone.is_aware(paid_at):
        raise ValidationError({
            "paid_at": "Payment date and time must include a timezone."
        })

    amount = _to_decimal(amount, "amount")

    if payment_method not in SupplierPayment.PaymentMethod.values:
        raise ValidationError({"payment_method": "Invalid payment method."})

    # Lock order: supplier (NO KEY UPDATE) -> purchases -> payment.
    # NO KEY UPDATE serializes payments of one supplier (replay check and
    # auto-allocation) without blocking FK inserts that reference the
    # supplier (purchases, milk entries).
    supplier = (
        Supplier.objects.select_for_update(no_key=True)
        .filter(pk=supplier_id, business_id=business_id)
        .first()
    )

    if client_ref:
        replay = SupplierPayment.objects.filter(
            business_id=business_id, client_ref=client_ref
        ).first()

        if replay is not None:
            return _replay_or_conflict(
                replay,
                supplier_id=supplier_id,
                payment_number=payment_number,
                paid_at=paid_at,
                amount=amount,
                payment_method=payment_method,
                reference_number=reference_number,
                notes=notes,
                allocations=None if auto_allocate else allocations or [],
            )

    if supplier is None or not supplier.is_active or supplier.is_deleted:
        raise ValidationError(
            "Supplier does not exist, is inactive, or belongs to another business."
        )

    if SupplierPayment.objects.filter(
        business_id=business_id, payment_number=payment_number
    ).exists():
        raise ValidationError({
            "payment_number": "This payment number is already used."
        })

    if auto_allocate:
        allocations = _plan_auto_allocation(
            business_id=business_id,
            supplier_id=supplier_id,
            amount=amount,
        )

    allocations = allocations or []
    validated_allocations = []
    total_allocated = Decimal("0.00")
    seen_purchase_ids = set()

    # Validate requested allocations before creating the payment.
    for item in allocations:
        purchase_id = item.get("purchase_id")
        allocation_amount = _to_decimal(
            item.get("amount"), "allocation_amount"
        )

        if purchase_id in seen_purchase_ids:
            raise ValidationError(
                "A purchase can appear only once per payment."
            )

        seen_purchase_ids.add(purchase_id)
        total_allocated += allocation_amount
        validated_allocations.append(
            {
                "purchase_id": purchase_id,
                "amount": allocation_amount,
            }
        )

    if total_allocated > amount:
        raise ValidationError(
            "Allocated amount cannot exceed the payment amount."
        )

    # Lock the allocated purchases (pk order) to serialize allocations.
    purchase_ids = sorted(
        str(item["purchase_id"]) for item in validated_allocations
    )

    purchases = {}
    if purchase_ids:
        locked_purchases = list(
            SupplierPurchase.objects.select_for_update()
            .filter(
                pk__in=purchase_ids,
                business_id=business_id,
                supplier_id=supplier_id,
            )
            .order_by("pk")
        )
        purchases = {str(p.pk): p for p in locked_purchases}

        if len(purchases) != len(set(purchase_ids)):
            raise ValidationError(
                "One or more purchases do not belong to this supplier "
                "and business."
            )

    for item in validated_allocations:
        purchase = purchases[str(item["purchase_id"])]

        if purchase.status != SupplierPurchase.Status.RECEIVED:
            raise ValidationError(
                f"Purchase {purchase.purchase_number} must be received "
                "before payment can be allocated."
            )

        outstanding = purchase.net_amount - _allocated_to_purchase(
            business_id, purchase
        )

        if item["amount"] > outstanding:
            raise ValidationError(
                f"Allocation exceeds the outstanding amount for "
                f"purchase {purchase.purchase_number}."
            )

    try:
        # Savepoint: a unique-constraint race becomes a 400, not a 500.
        with transaction.atomic():
            payment = SupplierPayment.objects.create(
                business_id=business_id,
                supplier=supplier,
                payment_number=payment_number,
                paid_at=paid_at,
                amount=amount,
                payment_method=payment_method,
                reference_number=reference_number,
                notes=notes,
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

    for item in validated_allocations:
        SupplierPaymentAllocation.objects.create(
            business_id=business_id,
            payment=payment,
            purchase=purchases[str(item["purchase_id"])],
            amount=item["amount"],
        )

    payment.replayed = False
    return payment



def _find_payment(payment_id, business_id=None):
    try:
        queryset = SupplierPayment.objects.only("business_id")
        if business_id is not None:
            queryset = queryset.filter(business_id=business_id)
        return queryset.get(pk=payment_id)
    except (SupplierPayment.DoesNotExist, ValidationError, ValueError):
        raise ValidationError("Payment not found.")


@transaction.atomic
def allocate_supplier_advance(
    *,
    payment_id,
    purchase_id,
    amount,
    user,
    business_id=None,
):
    """
    Apply part of a payment's unallocated advance to a received purchase.

    Returns the allocation; `.replayed` is True when this payment is
    already allocated to this purchase for exactly this amount (a retry).
    Lock order: purchase -> payment (same as every other writer).
    """
    amount = _to_decimal(amount, "amount")

    # Identify the business, then validate membership. When the API passes
    # business_id the lookup is tenant-scoped; "not found" and "not yours"
    # give the same answer.
    payment_ref = _find_payment(payment_id, business_id)

    require_business_membership(
        user=user,
        business_id=payment_ref.business_id,
        allowed_roles=ALLOWED_ROLES,
    )

    purchase = (
        SupplierPurchase.objects.select_for_update()
        .filter(pk=purchase_id, business_id=payment_ref.business_id)
        .first()
    )

    if purchase is None:
        raise ValidationError(
            "Purchase not found for this payment's business."
        )

    payment = SupplierPayment.objects.select_for_update().get(
        pk=payment_id,
        business_id=payment_ref.business_id,
    )

    if payment.supplier_id != purchase.supplier_id:
        raise ValidationError(
            "Payment and purchase must belong to the same supplier."
        )

    if purchase.status != SupplierPurchase.Status.RECEIVED:
        raise ValidationError(
            "Only received purchases can receive allocations."
        )

    existing = SupplierPaymentAllocation.objects.filter(
        payment=payment, purchase=purchase
    ).first()

    if existing is not None:
        if (
            existing.amount == amount
            and not existing.unwinds.exists()
        ):
            existing.replayed = True
            return existing

        raise ValidationError(
            "This payment is already allocated to this purchase."
        )

    amounts = get_payment_amounts(payment)

    if amounts["net"] <= 0:
        raise ValidationError("A reversed payment cannot be allocated.")

    if amount > amounts["unallocated"]:
        raise ValidationError(
            "Allocation exceeds the remaining advance."
        )

    outstanding = purchase.net_amount - allocated_to_purchase(
        payment.business_id, purchase
    )

    if amount > outstanding:
        raise ValidationError(
            "Allocation exceeds the purchase's outstanding amount."
        )

    allocation = SupplierPaymentAllocation.objects.create(
        business_id=payment.business_id,
        payment=payment,
        purchase=purchase,
        amount=amount,
    )
    allocation.replayed = False
    return allocation


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


@transaction.atomic
def reverse_supplier_payment(
    *,
    payment_id,
    reason,
    user,
    business_id=None,
    amount=None,
    client_ref=None,
):
    """
    Reverse all (amount=None) or part of what is left of a payment.

    The reversed amount comes out of the unallocated advance first; only
    the rest unwinds allocations, newest first, each recorded as an
    explicit SupplierPaymentAllocationReversal row. Nothing is edited.
    Returns the reversal; `.replayed` is True for an identical client_ref
    retry. `.unwound` lists the (allocation, amount) pairs taken back.

    Locking: only the payment row (allocate_supplier_advance also locks
    purchase -> payment, so the two serialize on the payment). Unwinding
    needs no purchase lock: it only frees purchase capacity, and a purchase
    cancelled meanwhile just makes that allocation irrelevant.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({
            "reason": "A reason is required to reverse a payment."
        })

    if amount is not None:
        amount = _to_decimal(amount, "amount")

    client_ref = (client_ref or "").strip() or None

    # Read the business ID before authorization (tenant-scoped if given).
    payment_ref = _find_payment(payment_id, business_id)

    require_business_membership(
        user=user,
        business_id=payment_ref.business_id,
        allowed_roles=ALLOWED_ROLES,
    )

    # Lock the payment so concurrent reversals/allocations of it serialize.
    payment = SupplierPayment.objects.select_for_update().get(
        pk=payment_id,
        business_id=payment_ref.business_id,
    )

    if client_ref:
        replay = SupplierPaymentReversal.objects.filter(
            business_id=payment.business_id, client_ref=client_ref
        ).first()

        if replay is not None:
            replay = _reversal_replay_or_conflict(
                replay, payment_id=payment.pk, amount=amount, reason=reason
            )
            replay.unwound = _unwound_rows(replay)
            return replay

    amounts = get_payment_amounts(payment)

    if amounts["net"] <= 0:
        raise ValidationError("This payment has already been fully reversed.")

    if amount is None:
        amount = amounts["net"]

    if amount > amounts["net"]:
        raise ValidationError(
            "Cannot reverse more than the remaining payment amount "
            f"({amounts['net']})."
        )

    from_advance = min(amount, max(amounts["unallocated"], Decimal("0.00")))
    to_unwind = amount - from_advance

    try:
        # Savepoint: a unique-constraint race becomes a 409, not a 500.
        with transaction.atomic():
            reversal = SupplierPaymentReversal.objects.create(
                business_id=payment.business_id,
                payment=payment,
                amount=amount,
                reason=reason,
                # Never before the payment itself (future-dated payments).
                reversed_at=max(timezone.now(), payment.paid_at),
                client_ref=client_ref,
                created_by=user,
            )
    except IntegrityError:
        raise ClientRefConflict({
            "client_ref": "This reference was just used by another request."
        })

    if to_unwind > 0:
        allocations = (
            SupplierPaymentAllocation.objects.filter(
                payment=payment,
                purchase__status=SupplierPurchase.Status.RECEIVED,
            )
            .order_by("-created_at", "-pk")
        )

        for allocation in allocations:
            if to_unwind <= 0:
                break

            effective = allocation.amount - (
                allocation.unwinds.aggregate(t=Sum("amount"))["t"]
                or Decimal("0.00")
            )

            if effective <= 0:
                continue

            take = min(effective, to_unwind)
            SupplierPaymentAllocationReversal.objects.create(
                business_id=payment.business_id,
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


def _unwound_rows(reversal):
    return list(
        SupplierPaymentAllocationReversal.objects.filter(reversal=reversal)
        .select_related("allocation__purchase")
        .order_by("created_at", "pk")
    )
