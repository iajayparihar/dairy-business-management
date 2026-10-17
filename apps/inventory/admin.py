from django.contrib import admin

from apps.common.admin_mixins import ReadOnlyAdmin
from apps.inventory.models import ProductionBatch, ProductionLine, StockAdjustment, StockMovement


@admin.register(StockMovement)
class StockMovementAdmin(ReadOnlyAdmin):
    list_display = ("product", "movement_type", "quantity", "occurred_at", "created_at")
    list_filter = ("business", "movement_type")


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(ReadOnlyAdmin):
    list_display = ("adjustment_number", "business", "kind", "status",
                    "product", "quantity", "occurred_at")
    list_filter = ("business", "kind", "status")
    search_fields = ("adjustment_number", "client_ref")


@admin.register(ProductionBatch)
class ProductionBatchAdmin(ReadOnlyAdmin):
    list_display = ("batch_number", "business", "status", "produced_at")
    list_filter = ("business", "status")
    search_fields = ("batch_number", "client_ref")


@admin.register(ProductionLine)
class ProductionLineAdmin(ReadOnlyAdmin):
    list_display = ("batch", "kind", "product", "quantity", "unit", "stock_shortfall")
