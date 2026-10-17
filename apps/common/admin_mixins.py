from django.contrib import admin


class ReadOnlyAdmin(admin.ModelAdmin):
    """Ledger rows are a record: view only, never add/change/delete."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
