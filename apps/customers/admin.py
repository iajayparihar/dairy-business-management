from django.contrib import admin

from .models import Customer


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "customer_code",
        "name",
        "business",
        "customer_type",
        "phone",
        "is_active",
        "is_deleted",
    )
    list_filter = ("business", "customer_type", "is_active", "is_deleted")
    search_fields = ("customer_code", "name", "phone", "email")
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at", "deleted_by")