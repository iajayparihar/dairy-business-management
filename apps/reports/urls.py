from django.urls import path

from apps.reports import views

app_name = "reports"

B = "businesses/<uuid:business_id>/reports/"

urlpatterns = [
    path(B + "milk-balance/", views.MilkBalanceView.as_view(), name="milk-balance"),
    path(B + "closing-stock/", views.ClosingStockView.as_view(), name="closing-stock"),
    path(B + "dashboard/daily/", views.DailySummaryView.as_view(), name="daily-summary"),
    path(B + "dashboard/profit/", views.ProfitView.as_view(), name="profit"),
    path(B + "supplier-balances/", views.SupplierBalancesView.as_view(), name="supplier-balances"),
    path(B + "supplier-ledger/", views.SupplierLedgerView.as_view(), name="supplier-ledger"),
    path(B + "customer-outstanding/", views.CustomerOutstandingView.as_view(), name="customer-outstanding"),
    path(B + "stock/", views.StockReportView.as_view(), name="stock"),
    path(B + "product-sales/", views.ProductSalesView.as_view(), name="product-sales"),
    path(B + "expenses/", views.ExpenseReportView.as_view(), name="expenses"),
]
