from django.urls import path

from apps.inventory import views

app_name = "inventory"

B = "businesses/<uuid:business_id>/stock/"

urlpatterns = [
    path(B, views.StockBalanceListView.as_view(), name="stock-list"),
    path(B + "<uuid:product_id>/", views.StockBalanceDetailView.as_view(), name="stock-detail"),
]
