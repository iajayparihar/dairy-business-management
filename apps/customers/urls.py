
from django.urls import path

from apps.customers.views import (
    CustomerDetailAPIView,
    CustomerListCreateAPIView,
)

app_name = "customers"

urlpatterns = [
    path(
        "businesses/<uuid:business_id>/customers/",
        CustomerListCreateAPIView.as_view(),
        name="customer-list-create",
    ),
    path(
        "businesses/<uuid:business_id>/customers/<uuid:pk>/",
        CustomerDetailAPIView.as_view(),
        name="customer-detail",
    ),
]
