
from django.contrib import admin

from apps.deliveries.models import Delivery


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "delivery_date",
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

    readonly_fields = (
        "id",
        "line_total",
        "created_at",
        "updated_at",
    )
