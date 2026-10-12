
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.businesses.services import require_business_membership
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
)
from apps.purchases.models import SupplierPurchase


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
                purchase=purchase
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )

        outstanding = purchase.total_amount - already_allocated

        if item["amount"] > outstanding:
            raise ValidationError(
                f"Allocation exceeds the outstanding amount for "
                f"purchase {purchase.purchase_number}."
            )

    payment = SupplierPayment.objects.create(
        business_id=business_id,
        supplier_id=supplier_id,
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
