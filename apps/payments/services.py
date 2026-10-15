
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.businesses.services import require_business_membership
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)
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
):
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=ALLOWED_ROLES,
    )

    supplier = Supplier.objects.filter(
        pk=supplier_id,
        business_id=business_id,
        is_active=True,
        is_deleted=False,
    ).first()

    if supplier is None:
        raise ValidationError(
            "Supplier does not exist, is inactive, or belongs to another business."
        )

    if not timezone.is_aware(paid_at):
        raise ValidationError({
            "paid_at": "Payment date and time must include a timezone."
        })

    amount = _to_decimal(amount, "amount")

    if payment_method not in SupplierPayment.PaymentMethod.values:
        raise ValidationError({"payment_method": "Invalid payment method."})

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

    # Lock the supplier's purchase records to serialize allocations.
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

        
        already_allocated = (
            SupplierPaymentAllocation.objects.filter(
                business_id=business_id,
                purchase=purchase,
                payment__business_id=business_id,
                payment__supplier_id=supplier_id,
                payment__reversal__isnull=True,
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )


        outstanding = purchase.net_amount - already_allocated

        if item["amount"] > outstanding:
            raise ValidationError(
                f"Allocation exceeds the outstanding amount for "
                f"purchase {purchase.purchase_number}."
            )

    payment = SupplierPayment.objects.create(
        business_id=business_id,
        supplier=supplier,
        payment_number=payment_number,
        paid_at=paid_at,
        amount=amount,
        payment_method=payment_method,
        reference_number=reference_number,
        notes=notes,
        created_by=user,
    )

    for item in validated_allocations:
        SupplierPaymentAllocation.objects.create(
            business_id=business_id,
            payment=payment,
            purchase=purchases[str(item["purchase_id"])],
            amount=item["amount"],
        )

    return payment



@transaction.atomic
def allocate_supplier_advance(
    *,
    payment_id,
    purchase_id,
    amount,
    user,
):
    amount = _to_decimal(amount, "amount")

    # Identify the business, then validate membership.
    payment_ref = SupplierPayment.objects.only(
        "business_id"
    ).get(pk=payment_id)

    require_business_membership(
        user=user,
        business_id=payment_ref.business_id,
        allowed_roles=ALLOWED_ROLES,
    )

    # Lock the purchase first, matching the payment-recording
    # service's purchase-locking strategy.
    purchase_ref = SupplierPurchase.objects.only(
        "business_id"
    ).get(pk=purchase_id)

    if purchase_ref.business_id != payment_ref.business_id:
        raise ValidationError(
            "Payment and purchase must belong to the same business."
        )

    purchase = SupplierPurchase.objects.select_for_update().get(
        pk=purchase_id,
        business_id=payment_ref.business_id,
    )

    payment = SupplierPayment.objects.select_for_update().get(
        pk=payment_id,
        business_id=payment_ref.business_id,
    )

    if payment.supplier_id != purchase.supplier_id:
        raise ValidationError(
            "Payment and purchase must belong to the same supplier."
        )

    if SupplierPaymentReversal.objects.filter(
        payment=payment
    ).exists():
        raise ValidationError(
            "A reversed payment cannot be allocated."
        )

    if purchase.status != SupplierPurchase.Status.RECEIVED:
        raise ValidationError(
            "Only received purchases can receive allocations."
        )

    # Allocations on cancelled purchases no longer use up the advance
    # (the balance selector already ignores them).
    existing_allocations = list(
        SupplierPaymentAllocation.objects.filter(
            payment=payment,
            purchase__status=SupplierPurchase.Status.RECEIVED,
        ).select_related("purchase")
    )

    already_allocated_from_payment = sum(
        (item.amount for item in existing_allocations),
        Decimal("0.00"),
    )

    remaining_advance = payment.amount - already_allocated_from_payment

    if amount > remaining_advance:
        raise ValidationError(
            "Allocation exceeds the remaining advance."
        )

    
    already_allocated_to_purchase = (
        SupplierPaymentAllocation.objects.filter(
            business_id=payment.business_id,
            purchase=purchase,
            payment__business_id=payment.business_id,
            payment__supplier_id=payment.supplier_id,
            payment__reversal__isnull=True,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )


    outstanding = (
        purchase.net_amount - already_allocated_to_purchase
    )

    if amount > outstanding:
        raise ValidationError(
            "Allocation exceeds the purchase's outstanding amount."
        )

    if SupplierPaymentAllocation.objects.filter(
        payment=payment,
        purchase=purchase,
    ).exists():
        raise ValidationError(
            "This payment is already allocated to this purchase."
        )
    if SupplierPaymentReversal.objects.filter(
        payment=payment
    ).exists():
        raise ValidationError(
            "A reversed payment cannot be allocated."
        )
    return SupplierPaymentAllocation.objects.create(
        business_id=payment.business_id,
        payment=payment,
        purchase=purchase,
        amount=amount,
    )



@transaction.atomic
def reverse_supplier_payment(
    *,
    payment_id,
    reason,
    user,
):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({
            "reason": "A reason is required to reverse a payment."
        })

    # Read the business ID before authorization.
    payment_ref = SupplierPayment.objects.only(
        "business_id"
    ).get(pk=payment_id)

    require_business_membership(
        user=user,
        business_id=payment_ref.business_id,
        allowed_roles=ALLOWED_ROLES,
    )

    # Lock the payment so two requests cannot reverse it together.
    payment = SupplierPayment.objects.select_for_update().get(
        pk=payment_id,
        business_id=payment_ref.business_id,
    )

    if SupplierPaymentReversal.objects.filter(
        payment=payment
    ).exists():
        raise ValidationError("This payment has already been reversed.")

    reversal = SupplierPaymentReversal.objects.create(
        business_id=payment.business_id,
        payment=payment,
        amount=payment.amount,
        reason=reason,
        reversed_at=timezone.now(),
        created_by=user,
    )

    return reversal
