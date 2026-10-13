
from django.urls import path

from apps.purchases.api_views import (
    MilkCollectionDetailAPIView,
    MilkCollectionListCreateAPIView,
)

app_name = "purchases"

urlpatterns = [
    path(
        "businesses/<uuid:business_id>/suppliers/"
        "<uuid:supplier_id>/milk-collections/",
        MilkCollectionListCreateAPIView.as_view(),
        name="milk-collection-list-create",
    ),
    path(
        "businesses/<uuid:business_id>/suppliers/"
        "<uuid:supplier_id>/milk-collections/<uuid:collection_id>/",
        MilkCollectionDetailAPIView.as_view(),
        name="milk-collection-detail",
    ),
]
