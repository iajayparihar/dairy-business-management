from django.urls import path

from apps.sales import views

app_name = "sales"

B = "businesses/<uuid:business_id>/sales/"

urlpatterns = [
    path(B, views.SaleListCreateView.as_view(), name="sale-list-create"),
    path(B + "summary/", views.SaleSummaryView.as_view(), name="sale-summary"),
    path(B + "quick-customer/", views.QuickCustomerView.as_view(), name="quick-customer"),
    path(B + "<uuid:sale_id>/", views.SaleDetailView.as_view(), name="sale-detail"),
    path(B + "<uuid:sale_id>/cancel/", views.SaleCancelView.as_view(), name="sale-cancel"),
    path(B + "<uuid:sale_id>/correct/", views.SaleCorrectView.as_view(), name="sale-correct"),
]
