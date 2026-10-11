
from django.urls import path

from apps.suppliers.views import (
    SupplierDetailAPIView,
    SupplierListCreateAPIView,
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
]
