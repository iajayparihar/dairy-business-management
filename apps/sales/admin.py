from django.contrib import admin

from apps.common.admin_mixins import ReadOnlyAdmin
from apps.sales.models import Sale, SaleLine


@admin.register(Sale)
class SaleAdmin(ReadOnlyAdmin):
    list_display = ("sale_number", "business", "payment_mode", "status",
                    "total_amount", "sold_at")
    list_filter = ("business", "payment_mode", "status")
    search_fields = ("sale_number", "client_ref")


@admin.register(SaleLine)
class SaleLineAdmin(ReadOnlyAdmin):
    list_display = ("sale", "position", "description", "quantity",
                    "unit_price", "line_total", "stock_shortfall")
