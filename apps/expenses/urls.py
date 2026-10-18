from django.urls import path

from apps.expenses import views

app_name = "expenses"

B = "businesses/<uuid:business_id>/"

urlpatterns = [
    path(B + "expense-categories/", views.CategoryListCreateView.as_view(), name="category-list-create"),
    path(B + "expense-categories/<uuid:category_id>/", views.CategoryDetailView.as_view(), name="category-detail"),
    path(B + "expenses/", views.ExpenseListCreateView.as_view(), name="expense-list-create"),
    path(B + "expenses/summary/", views.ExpenseSummaryView.as_view(), name="expense-summary"),
    path(B + "expenses/<uuid:expense_id>/", views.ExpenseDetailView.as_view(), name="expense-detail"),
    path(B + "expenses/<uuid:expense_id>/cancel/", views.ExpenseCancelView.as_view(), name="expense-cancel"),
    path(B + "expenses/<uuid:expense_id>/correct/", views.ExpenseCorrectView.as_view(), name="expense-correct"),
    path(B + "employees/", views.EmployeeListCreateView.as_view(), name="employee-list-create"),
    path(B + "employees/<uuid:employee_id>/", views.EmployeeDetailView.as_view(), name="employee-detail"),
    path(B + "employees/<uuid:employee_id>/balance/", views.EmployeeBalanceView.as_view(), name="employee-balance"),
    path(B + "employees/<uuid:employee_id>/statement/", views.EmployeeStatementView.as_view(), name="employee-statement"),
    path(B + "employees/<uuid:employee_id>/payments/", views.EmployeePaymentsView.as_view(), name="employee-payments"),
    path(B + "employees/<uuid:employee_id>/adjustments/", views.AdjustmentCreateView.as_view(), name="employee-adjustments"),
    path(B + "salary/accruals/", views.AccrualListView.as_view(), name="accrual-list"),
    path(B + "salary/accruals/generate/", views.GenerateSalaryView.as_view(), name="accrual-generate"),
    path(B + "salary/accruals/<uuid:accrual_id>/cancel/", views.AccrualCancelView.as_view(), name="accrual-cancel"),
    path(B + "salary-payments/<uuid:payment_id>/", views.SalaryPaymentDetailView.as_view(), name="salary-payment-detail"),
    path(B + "salary-payments/<uuid:payment_id>/cancel/", views.SalaryPaymentCancelView.as_view(), name="salary-payment-cancel"),
]
