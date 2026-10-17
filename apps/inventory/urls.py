from django.urls import path

from apps.inventory import views

app_name = "inventory"

B = "businesses/<uuid:business_id>/stock/"

urlpatterns = [
    path(B + "adjustments/", views.AdjustmentListCreateView.as_view(), name="adjustment-list-create"),
    path(B + "adjustments/<uuid:adjustment_id>/", views.AdjustmentDetailView.as_view(), name="adjustment-detail"),
    path(B + "adjustments/<uuid:adjustment_id>/cancel/", views.AdjustmentCancelView.as_view(), name="adjustment-cancel"),
    path(B + "production/", views.ProductionListCreateView.as_view(), name="production-list-create"),
    path(B + "production/<uuid:batch_id>/", views.ProductionDetailView.as_view(), name="production-detail"),
    path(B + "production/<uuid:batch_id>/cancel/", views.ProductionCancelView.as_view(), name="production-cancel"),
    path(B + "production/<uuid:batch_id>/correct/", views.ProductionCorrectView.as_view(), name="production-correct"),
    path(B, views.StockBalanceListView.as_view(), name="stock-list"),
    path(B + "<uuid:product_id>/", views.StockBalanceDetailView.as_view(), name="stock-detail"),
]
