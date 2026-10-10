from django.contrib import admin

from .models import Supplier


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = (
        "supplier_code",
        "name",
        "business",
        "supplier_type",
        "phone",
        "is_active",
        "is_deleted",
    )
    list_filter = ("business", "supplier_type", "is_active", "is_deleted")
    search_fields = ("supplier_code", "name", "phone", "email")
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at", "deleted_by")