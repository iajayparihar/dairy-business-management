
from django.urls import path

from apps.deliveries.views import (
    DeliveryCancelView,
    DeliveryDetailView,
    DeliveryListCreateView,
)

app_name = "deliveries"

urlpatterns = [
    path(
        "",
        DeliveryListCreateView.as_view(),
        name="delivery-list-create",
    ),
    path(
        "<uuid:pk>/",
        DeliveryDetailView.as_view(),
        name="delivery-detail",
    ),
    path(
        "<uuid:pk>/cancel/",
        DeliveryCancelView.as_view(),
        name="delivery-cancel",
    ),
]
