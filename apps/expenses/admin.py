from django.contrib import admin

from apps.common.admin_mixins import ReadOnlyAdmin
from apps.expenses.models import (
    Employee, Expense, ExpenseCategory, SalaryAccrual, SalaryPayment,
)


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(ReadOnlyAdmin):
    list_display = ("name", "business", "is_active")
    list_filter = ("business", "is_active")


@admin.register(Expense)
class ExpenseAdmin(ReadOnlyAdmin):
    list_display = ("expense_number", "business", "category", "amount",
                    "expense_date", "status")
    list_filter = ("business", "status")
    search_fields = ("expense_number", "client_ref", "payee")


@admin.register(Employee)
class EmployeeAdmin(ReadOnlyAdmin):
    list_display = ("code", "name", "business", "monthly_salary", "is_active")
    list_filter = ("business", "is_active")
    search_fields = ("code", "name")


@admin.register(SalaryAccrual)
class SalaryAccrualAdmin(ReadOnlyAdmin):
    list_display = ("employee", "kind", "month", "amount", "status")
    list_filter = ("business", "kind", "status")


@admin.register(SalaryPayment)
class SalaryPaymentAdmin(ReadOnlyAdmin):
    list_display = ("payment_number", "employee", "amount", "paid_at", "status")
    list_filter = ("business", "status")
    search_fields = ("payment_number", "client_ref")
