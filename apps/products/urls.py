
from django.urls import path

from apps.products.views import (
    ProductCategoryListCreateAPIView,
    ProductDetailAPIView,
    ProductListCreateAPIView,
)

app_name = "products"

urlpatterns = [
    path(
        "businesses/<uuid:business_id>/categories/",
        ProductCategoryListCreateAPIView.as_view(),
        name="category-list-create",
    ),
    path(
        "businesses/<uuid:business_id>/products/",
        ProductListCreateAPIView.as_view(),
        name="product-list-create",
    ),
    path(
        "businesses/<uuid:business_id>/products/<uuid:pk>/",
        ProductDetailAPIView.as_view(),
        name="product-detail",
    ),
]
