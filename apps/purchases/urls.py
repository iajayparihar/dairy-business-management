
from django.urls import path

from apps.purchases.api_views import (
    PurchaseAdjustmentListAPIView,
    PurchaseCorrectionAPIView,
    PurchaseReturnAPIView,
    PurchaseCancelAPIView,
    PurchaseDetailAPIView,
    PurchaseListCreateAPIView,
    PurchaseReceiveAPIView,
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
    path(
        "businesses/<uuid:business_id>/purchases/",
        PurchaseListCreateAPIView.as_view(),
        name="purchase-list-create",
    ),
    path(
        "businesses/<uuid:business_id>/purchases/<uuid:pk>/",
        PurchaseDetailAPIView.as_view(),
        name="purchase-detail",
    ),
    path(
        "businesses/<uuid:business_id>/purchases/<uuid:pk>/receive/",
        PurchaseReceiveAPIView.as_view(),
        name="purchase-receive",
    ),
    path(
        "businesses/<uuid:business_id>/purchases/<uuid:pk>/cancel/",
        PurchaseCancelAPIView.as_view(),
        name="purchase-cancel",
    ),
    path(
        "businesses/<uuid:business_id>/purchases/<uuid:pk>/returns/",
        PurchaseReturnAPIView.as_view(),
        name="purchase-return",
    ),
    path(
        "businesses/<uuid:business_id>/purchases/<uuid:pk>/corrections/",
        PurchaseCorrectionAPIView.as_view(),
        name="purchase-correction",
    ),
    path(
        "businesses/<uuid:business_id>/purchases/<uuid:pk>/adjustments/",
        PurchaseAdjustmentListAPIView.as_view(),
        name="purchase-adjustments",
    ),
]
