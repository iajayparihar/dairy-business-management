
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated

from apps.billing.guards import customer_has_records
from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
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

    def get_queryset(self):
        return scope_to_business(
            Customer.objects.all(),
            self.request.business,
        )


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
                    "This customer has deliveries, bills or payments and "
                    "cannot be deleted. Mark the customer inactive instead."
                )
            })
        instance.delete()
