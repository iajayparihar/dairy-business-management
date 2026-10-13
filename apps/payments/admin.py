from django.contrib import admin
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)

admin.site.register(SupplierPayment)
admin.site.register(SupplierPaymentAllocation)
admin.site.register(SupplierPaymentReversal)