from django.contrib import admin

from apps.common.admin_mixins import ReadOnlyAdmin

from apps.billing.models import (
    CustomerBill,
    CustomerBillCancelledLine,
    CustomerBillLine,
    CustomerPayment,
    CustomerPaymentAllocation,
    CustomerPaymentAllocationReversal,
    CustomerPaymentReversal,
)





@admin.register(CustomerBill)
class CustomerBillAdmin(ReadOnlyAdmin):
    list_display = (
        "bill_number",
        "customer",
        "bill_type",
        "status",
        "period_start",
        "period_end",
        "total_amount",
    )
    list_filter = ("status", "bill_type", "business")
    search_fields = (
        "bill_number",
        "customer__name",
        "customer__customer_code",
    )


@admin.register(CustomerPayment)
class CustomerPaymentAdmin(ReadOnlyAdmin):
    list_display = (
        "payment_number",
        "customer",
        "paid_at",
        "amount",
        "payment_method",
    )
    list_filter = ("payment_method", "business")
    search_fields = (
        "payment_number",
        "customer__name",
        "customer__customer_code",
    )


for model in (
    CustomerBillCancelledLine,
    CustomerBillLine,
    CustomerPaymentAllocation,
    CustomerPaymentReversal,
    CustomerPaymentAllocationReversal,
):
    admin.site.register(model, ReadOnlyAdmin)
