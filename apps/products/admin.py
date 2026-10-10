
from django.contrib import admin

from .models import Product, ProductCategory, ProductPrice


@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "is_active", "is_deleted")
    list_filter = ("business", "is_active", "is_deleted")
    search_fields = ("name",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "sku",
        "name",
        "business",
        "category",
        "unit",
        "is_active",
        "track_stock",
        "is_deleted",
    )
    list_filter = ("business", "unit", "is_active", "track_stock")
    search_fields = ("sku", "name")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(ProductPrice)
class ProductPriceAdmin(admin.ModelAdmin):
    list_display = (
        "product",
        "business",
        "price_type",
        "unit_price",
        "effective_from",
        "effective_to",
        "is_deleted",
    )
    list_filter = ("business", "price_type", "is_deleted")
    search_fields = ("product__name", "product__sku")
    readonly_fields = ("id", "created_at", "updated_at")
