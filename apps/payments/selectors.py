from decimal import Decimal

from django.db.models import Sum

from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
)
from apps.purchases.models import SupplierPurchase


def get_supplier_balance(*, business_id, supplier_id):
    purchases = SupplierPurchase.objects.filter(
        business_id=business_id,
        supplier_id=supplier_id,
        status=SupplierPurchase.Status.RECEIVED,
    )

    total_purchases = (
        purchases.aggregate(total=Sum("total_amount"))["total"]
        or Decimal("0.00")
    )

    valid_allocations = SupplierPaymentAllocation.objects.filter(
        business_id=business_id,
        payment__business_id=business_id,
        payment__supplier_id=supplier_id,
        payment__reversal__isnull=True,
        purchase__business_id=business_id,
        purchase__supplier_id=supplier_id,
        purchase__status=SupplierPurchase.Status.RECEIVED,
    )

    allocated_payments = (
        valid_allocations.aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )

    valid_payments = SupplierPayment.objects.filter(
        business_id=business_id,
        supplier_id=supplier_id,
        reversal__isnull=True,
    )

    total_payments = (
        valid_payments.aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )

    available_credit = total_payments - allocated_payments

    return {
        "total_purchases": total_purchases,
        "allocated_payments": allocated_payments,
        "outstanding_amount": total_purchases - allocated_payments,
        "available_credit": max(available_credit, Decimal("0.00")),
    }