from django.urls import path

from apps.reports import views

app_name = "reports"

B = "businesses/<uuid:business_id>/reports/"

urlpatterns = [
    path(B + "milk-balance/", views.MilkBalanceView.as_view(), name="milk-balance"),
    path(B + "closing-stock/", views.ClosingStockView.as_view(), name="closing-stock"),
]
