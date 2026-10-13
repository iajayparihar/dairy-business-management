
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date

from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.purchases.api_serializers import (
    MilkCollectionCreateSerializer,
    MilkCollectionReadSerializer,
)
from apps.purchases.models import MilkCollection
from apps.purchases.services import create_milk_collection
from apps.suppliers.models import Supplier


def raise_api_validation_error(exc):
    detail = getattr(exc, "message_dict", None)
    if detail is None:
        detail = exc.messages
    raise ValidationError(detail)


class MilkCollectionPermissionMixin:
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

    def get_supplier(self):
        return get_object_or_404(
            Supplier.objects.filter(
                business_id=self.request.business.id,
                is_active=True,
                is_deleted=False,
            ),
            pk=self.kwargs["supplier_id"],
        )


class MilkCollectionListCreateAPIView(
    MilkCollectionPermissionMixin,
    generics.ListCreateAPIView,
):
    def get_queryset(self):
        supplier = self.get_supplier()

        queryset = MilkCollection.objects.filter(
            business_id=self.request.business.id,
            supplier_id=supplier.id,
        ).select_related("rate_record", "purchase")

        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")

        if start_date:
            parsed_start = parse_date(start_date)
            if parsed_start is None:
                raise ValidationError({
                    "start_date": "Use YYYY-MM-DD format."
                })
            queryset = queryset.filter(
                collected_at__date__gte=parsed_start,
            )

        if end_date:
            parsed_end = parse_date(end_date)
            if parsed_end is None:
                raise ValidationError({
                    "end_date": "Use YYYY-MM-DD format."
                })
            queryset = queryset.filter(
                collected_at__date__lte=parsed_end,
            )

        if start_date and end_date and parsed_start > parsed_end:
            raise ValidationError({
                "end_date": "Must be on or after start_date."
            })

        return queryset.order_by("-collected_at", "-created_at")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return MilkCollectionCreateSerializer
        return MilkCollectionReadSerializer

    def create(self, request, *args, **kwargs):
        input_serializer = self.get_serializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        supplier = self.get_supplier()
        data = input_serializer.validated_data

        try:
            collection = create_milk_collection(
                business_id=request.business.id,
                supplier_id=supplier.id,
                collection_number=data["collection_number"],
                milk_type=data["milk_type"],
                collected_at=data["collected_at"],
                litres=data["litres"],
                fat=data["fat"],
                user=request.user,
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)
        except IntegrityError:
            # Database constraints remain the final duplicate guard.
            raise ValidationError({
                "collection_number": (
                    "This collection or its linked purchase number "
                    "already exists."
                )
            })

        output = MilkCollectionReadSerializer(collection)
        return Response(
            output.data,
            status=status.HTTP_201_CREATED,
        )


class MilkCollectionDetailAPIView(
    MilkCollectionPermissionMixin,
    generics.RetrieveAPIView,
):
    serializer_class = MilkCollectionReadSerializer
    lookup_url_kwarg = "collection_id"

    def get_queryset(self):
        supplier = self.get_supplier()

        return MilkCollection.objects.filter(
            business_id=self.request.business.id,
            supplier_id=supplier.id,
        ).select_related("rate_record", "purchase")
