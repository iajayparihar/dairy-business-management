from django.contrib import admin
from apps.purchases.models import SupplierPurchase, MilkCollection

admin.site.register(SupplierPurchase)
admin.site.register(MilkCollection)