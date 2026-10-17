from django.urls import path

from apps.payments.views import (
    SupplierAccountStatementAPIView,
    SupplierAccountStatementPrintAPIView,
    SupplierBalanceAPIView,
    SupplierPaymentAllocateAPIView,
    SupplierPaymentDetailAPIView,
    SupplierPaymentListCreateAPIView,
    SupplierPaymentReverseAPIView,
    SupplierPaySummaryAPIView,
)

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
    path(
        "businesses/<uuid:business_id>/suppliers/"
        "<uuid:supplier_id>/balance/",
        SupplierBalanceAPIView.as_view(),
        name="supplier-balance",
    ),
    path(
        "businesses/<uuid:business_id>/payments/<uuid:payment_id>/",
        SupplierPaymentDetailAPIView.as_view(),
        name="supplier-payment-detail",
    ),
    path(
        "businesses/<uuid:business_id>/payments/<uuid:payment_id>/reverse/",
        SupplierPaymentReverseAPIView.as_view(),
        name="supplier-payment-reverse",
    ),
    path(
        "businesses/<uuid:business_id>/payments/<uuid:payment_id>/allocate/",
        SupplierPaymentAllocateAPIView.as_view(),
        name="supplier-payment-allocate",
    ),
    path(
        "businesses/<uuid:business_id>/suppliers/"
        "<uuid:supplier_id>/pay-summary/",
        SupplierPaySummaryAPIView.as_view(),
        name="supplier-pay-summary",
    ),
    path(
        "businesses/<uuid:business_id>/suppliers/"
        "<uuid:supplier_id>/statement/print/",
        SupplierAccountStatementPrintAPIView.as_view(),
        name="supplier-account-statement-print",
    ),
]
