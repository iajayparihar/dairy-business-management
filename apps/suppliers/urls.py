
from django.urls import path

from apps.suppliers.views import (
    SupplierDetailAPIView,
    SupplierListCreateAPIView,
)
from apps.suppliers.api_views import (
    SupplierMilkRateCorrectAPIView,
    SupplierMilkRateListCreateAPIView,
)

app_name = "suppliers"

urlpatterns = [
    path(
        "businesses/<uuid:business_id>/suppliers/",
        SupplierListCreateAPIView.as_view(),
        name="supplier-list-create",
    ),
    path(
        "businesses/<uuid:business_id>/suppliers/<uuid:pk>/",
        SupplierDetailAPIView.as_view(),
        name="supplier-detail",
    ),
    path(
        "businesses/<uuid:business_id>/suppliers/"
        "<uuid:supplier_id>/milk-rates/",
        SupplierMilkRateListCreateAPIView.as_view(),
        name="supplier-milk-rate-list-create",
    ),
    path(
        "businesses/<uuid:business_id>/suppliers/"
        "<uuid:supplier_id>/milk-rates/correct/",
        SupplierMilkRateCorrectAPIView.as_view(),
        name="supplier-milk-rate-correct",
    ),
]
