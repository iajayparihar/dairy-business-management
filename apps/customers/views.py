import re


from django.db.models import Q
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated

from apps.billing.guards import customer_has_records
from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.common.pagination import StandardPagination
from apps.common.querysets import scope_to_business
from apps.customers.models import Customer
from apps.customers.serializers import CustomerSerializer


class BusinessScopedCustomerAPIViewMixin:
    permission_classes = [IsAuthenticated, IsBusinessMember]

    def get_permissions(self):
        if self.request.method not in ("GET", "HEAD", "OPTIONS"):
            self.required_business_roles = [
                BusinessMembership.Role.OWNER,
                BusinessMembership.Role.MANAGER,
            ]
        else:
            self.required_business_roles = None

        return super().get_permissions()


class CustomerListCreateAPIView(
    BusinessScopedCustomerAPIViewMixin,
    generics.ListCreateAPIView,
):
    serializer_class = CustomerSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        params = self.request.query_params
        # unique code as tie-break: pages must not skip or repeat equal names
        customers = scope_to_business(
            Customer.objects.all(), self.request.business
        ).order_by("name", "customer_code")
        active = params.get("is_active", "").lower()
        if active in ("true", "false"):
            customers = customers.filter(is_active=active == "true")
        elif active:
            raise ValidationError({"is_active": "Use true or false."})
        kind = params.get("customer_type", "")
        if kind:
            if kind not in Customer.CustomerType.values:
                raise ValidationError({"customer_type": "Unknown customer type."})
            customers = customers.filter(customer_type=kind)
        term = params.get("search", "").strip()
        if term:
            # phones are stored without spaces / dashes, so search them the same way
            digits = re.sub(r"[\s\-().]", "", term)
            customers = customers.filter(
                Q(name__icontains=term) | Q(customer_code__icontains=term)
                | Q(phone__icontains=digits)
            )[:20]  # counter lookup: top 20 matches as a plain list (see paginate_queryset)
        return customers

    def paginate_queryset(self, queryset):
        # The counter's type-ahead search keeps its documented shape: a bare list.
        if self.request.query_params.get("search", "").strip():
            return None
        return super().paginate_queryset(queryset)


class CustomerDetailAPIView(
    BusinessScopedCustomerAPIViewMixin,
    generics.RetrieveUpdateDestroyAPIView,
):
    serializer_class = CustomerSerializer
    lookup_field = "pk"

    def get_queryset(self):
        return scope_to_business(
            Customer.objects.all(),
            self.request.business,
        )

    def perform_destroy(self, instance):
        if customer_has_records(instance):
            raise ValidationError({
                "detail": (
                    "This customer has deliveries, sales, bills or payments and "
                    "cannot be deleted. Mark the customer inactive instead."
                )
            })
        instance.soft_delete(self.request.user)
