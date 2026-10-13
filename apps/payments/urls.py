from django.urls import path

from apps.payments.views import SupplierAccountStatementAPIView

app_name = "payments"

urlpatterns = [
    path(
        "businesses/<uuid:business_id>/suppliers/"
        "<uuid:supplier_id>/statement/",
        SupplierAccountStatementAPIView.as_view(),
        name="supplier-account-statement",
    ),
]