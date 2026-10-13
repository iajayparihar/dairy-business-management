from django.urls import path

from apps.payments.views import SupplierAccountStatementAPIView, SupplierPaymentListCreateAPIView

app_name = "payments"

urlpatterns = [
    path(
        "businesses/<uuid:business_id>/suppliers/"
        "<uuid:supplier_id>/payments/",
        SupplierPaymentListCreateAPIView.as_view(),
        name="supplier-payment-list-create",
    ),
    path(
        "businesses/<uuid:business_id>/suppliers/"
        "<uuid:supplier_id>/statement/",
        SupplierAccountStatementAPIView.as_view(),
        name="supplier-account-statement",
    ),
]