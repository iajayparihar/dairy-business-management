
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
    list_display = (
        "collection_number",
        "business",
        "supplier",
        "collected_at",
        "litres",
        "amount",
        "created_by",
    )
    list_filter = ("business", "milk_type")
    search_fields = (
        "collection_number",
        "supplier__name",
    )
    readonly_fields = ("created_by", "created_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
