
from django.urls import path

from apps.deliveries.views import DeliveryListCreateView

app_name = "deliveries"

urlpatterns = [
    path(
        "",
        DeliveryListCreateView.as_view(),
        name="delivery-list-create",
    ),
]
