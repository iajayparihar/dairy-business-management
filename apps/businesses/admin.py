
from django.contrib import admin

from .models import Business, BusinessMembership


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "phone",
        "is_active",
        "created_at",
    )
    search_fields = ("name", "slug", "phone", "email")
    list_filter = ("is_active", "currency")
    readonly_fields = ("id", "created_at", "updated_at")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(BusinessMembership)
class BusinessMembershipAdmin(admin.ModelAdmin):
    list_display = (
        "business",
        "user",
        "role",
        "is_active",
        "joined_at",
    )
    list_filter = ("role", "is_active")
    search_fields = (
        "business__name",
        "user__username",
        "user__email",
    )
    readonly_fields = ("id", "joined_at", "updated_at")
