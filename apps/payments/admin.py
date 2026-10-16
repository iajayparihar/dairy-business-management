from django.contrib import admin

from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentAllocationReversal,
    SupplierPaymentReversal,
)


class LedgerReadOnlyAdmin(admin.ModelAdmin):
    """Ledger rows are append-only: view only, never add/change/delete."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
    SupplierPaymentAllocationReversal,
):
    admin.site.register(model, LedgerReadOnlyAdmin)
