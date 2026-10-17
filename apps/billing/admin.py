from django.contrib import admin

from apps.billing.models import (
    CustomerBill,
    CustomerBillLine,
    CustomerPayment,
    CustomerPaymentAllocation,
    CustomerPaymentAllocationReversal,
    CustomerPaymentReversal,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Billing rows are a financial record: view only, never add/change/delete."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
    CustomerBillLine,
    CustomerPaymentAllocation,
    CustomerPaymentReversal,
    CustomerPaymentAllocationReversal,
):
    admin.site.register(model, ReadOnlyAdmin)
