
from django.contrib import admin

from apps.deliveries.models import Delivery


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "delivery_date",
        "shift",
        "status",
        "business",
        "customer",
        "product",
        "quantity",
        "unit_price",
        "line_total",
        "created_by",
    )

    list_filter = ("business", "delivery_date", "product")
    search_fields = (
        "customer__name",
        "customer__customer_code",
        "product__name",
    )

    # Deliveries feed frozen bills: change them only through the services.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    readonly_fields = (
        "id",
        "line_total",
        "created_at",
        "updated_at",
    )
