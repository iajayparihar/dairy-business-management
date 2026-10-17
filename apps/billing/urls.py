from django.urls import path

from apps.billing import views

app_name = "billing"

B = "businesses/<uuid:business_id>/"

urlpatterns = [
    path(B + "customers/<uuid:customer_id>/payments/",
         views.CustomerPaymentListCreateView.as_view(), name="payment-list-create"),
    path(B + "customers/<uuid:customer_id>/balance/",
         views.CustomerBalanceView.as_view(), name="customer-balance"),
    path(B + "customers/<uuid:customer_id>/statement/",
         views.CustomerStatementView.as_view(), name="customer-statement"),
    path(B + "customers/<uuid:customer_id>/bills/",
         views.CustomerBillCreateView.as_view(), name="customer-bill-create"),
    path(B + "customers/<uuid:customer_id>/opening-balance/",
         views.OpeningBalanceView.as_view(), name="opening-balance"),
    path(B + "customer-payments/<uuid:payment_id>/",
         views.CustomerPaymentDetailView.as_view(), name="payment-detail"),
    path(B + "customer-payments/<uuid:payment_id>/reverse/",
         views.CustomerPaymentReverseView.as_view(), name="payment-reverse"),
    path(B + "customer-payments/<uuid:payment_id>/allocate/",
         views.CustomerPaymentAllocateView.as_view(), name="payment-allocate"),
    path(B + "bills/", views.BillListView.as_view(), name="bill-list"),
    path(B + "bills/generate/", views.BillGenerateView.as_view(), name="bill-generate"),
    path(B + "bills/<uuid:bill_id>/",
         views.BillDetailView.as_view(), name="bill-detail"),
    path(B + "bills/<uuid:bill_id>/finalize/",
         views.BillFinalizeView.as_view(), name="bill-finalize"),
    path(B + "bills/<uuid:bill_id>/void/",
         views.BillVoidView.as_view(), name="bill-void"),
    path(B + "bills/<uuid:bill_id>/pdf/",
         views.BillPdfView.as_view(), name="bill-pdf"),
]
