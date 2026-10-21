from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.users.models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    # Django's UserAdmin hashes passwords on create and shows the hash read-only;
    # a plain ModelAdmin stored whatever was typed as the raw "hash".
    list_display = ("id", "username", "email", "is_staff", "is_active")
    ordering = ("id",)
    fieldsets = DjangoUserAdmin.fieldsets + (("Contact", {"fields": ("phone_number",)}),)
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Contact", {"fields": ("phone_number",)}),
    )
