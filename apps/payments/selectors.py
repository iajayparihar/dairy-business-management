
from decimal import Decimal

from django.db.models import Sum

from apps.payments.models import SupplierPaymentAllocation
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

    allocated_payments = (
        SupplierPaymentAllocation.objects.filter(
            business_id=business_id,
            purchase__business_id=business_id,
            purchase__supplier_id=supplier_id,
            purchase__status=SupplierPurchase.Status.RECEIVED,
            payment__reversal__isnull=True,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )


    return {
        "total_purchases": total_purchases,
        "allocated_payments": allocated_payments,
        "outstanding_amount": total_purchases - allocated_payments,
    }
