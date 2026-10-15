
from django.contrib import admin

from apps.purchases.models import SupplierPurchase, MilkCollection


@admin.register(SupplierPurchase)
class SupplierPurchaseAdmin(admin.ModelAdmin):
    list_display = (
        "purchase_number",
        "business",
        "supplier",
        "purchased_at",
        "status",
        "total_amount",
        "created_by",
    )
    list_filter = ("business", "status")
    search_fields = (
        "purchase_number",
        "supplier__name",
        "business__name",
    )
    readonly_fields = (
        "created_by",
        "created_at",
        "updated_at",
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(MilkCollection)
class MilkCollectionAdmin(admin.ModelAdmin):
    """Ledger rows: read-only, never added or deleted here."""

    list_display = (
        "collection_number",
        "business",
        "supplier",
        "collected_at",
        "shift",
        "status",
        "litres",
        "amount",
    )
    list_filter = ("business", "milk_type", "shift", "status")
    search_fields = (
        "collection_number",
        "supplier__name",
    )

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
