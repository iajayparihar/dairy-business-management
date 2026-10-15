from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.businesses.permissions import IsBusinessMember
from apps.suppliers.models import Supplier
from apps.payments.statements import get_supplier_account_statement

from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from apps.businesses.models import BusinessMembership
from apps.payments.api_serializers import (
    SupplierPaymentCreateSerializer,
    SupplierPaymentReadSerializer,
)
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)
from apps.payments.services import record_supplier_payment
from apps.purchases.models import SupplierPurchase

class SupplierAccountStatementAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessMember]

    def get(self, request, business_id, supplier_id):
        supplier = get_object_or_404(
            Supplier.objects.filter(
                business_id=business_id,
                is_deleted=False,
            ),
            pk=supplier_id,
        )

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        try:
            statement = get_supplier_account_statement(
                business_id=business_id,
                supplier_id=supplier.id,
                start_date=start_date,
                end_date=end_date,
            )
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None)
            if detail is None:
                detail = exc.messages
            raise ValidationError(detail)

        return Response(statement, status=status.HTTP_200_OK)
    

class SupplierPaymentListCreateAPIView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessMember]

    def get(self, request, business_id, supplier_id):
        supplier = get_object_or_404(
            Supplier.objects.filter(
                business_id=request.business.id,
                is_deleted=False,
            ),
            pk=supplier_id,
        )

        payments = SupplierPayment.objects.filter(
            business_id=request.business.id,
            supplier=supplier,
        ).order_by("-paid_at", "-created_at")

        return Response(
            SupplierPaymentReadSerializer(payments, many=True).data
        )

    @transaction.atomic
    def post(self, request, business_id, supplier_id):
        membership = request.business_membership

        if membership.role not in (
            BusinessMembership.Role.OWNER,
            BusinessMembership.Role.MANAGER,
        ):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied(
                "Only owners and managers can record supplier payments."
            )

        supplier = get_object_or_404(
            Supplier.objects.filter(
                business_id=request.business.id,
                is_active=True,
                is_deleted=False,
            ),
            pk=supplier_id,
        )

        serializer = SupplierPaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Lock the supplier to serialize payment allocation decisions.
        supplier = Supplier.objects.select_for_update().get(
            pk=supplier.pk,
            business_id=request.business.id,
            is_active=True,
            is_deleted=False,
        )

        # Allocate payment to the oldest outstanding received purchases.
        # Any amount left after settling debt becomes supplier credit.
        purchases = list(
            SupplierPurchase.objects.select_for_update()
            .filter(
                business_id=request.business.id,
                supplier=supplier,
                status=SupplierPurchase.Status.RECEIVED,
                is_deleted=False,
            )
            .order_by("purchased_at", "pk")
        )

        allocations = []
        remaining = data["amount"]

        for purchase in purchases:
            if remaining <= Decimal("0.00"):
                break

            paid = (
                SupplierPaymentAllocation.objects.filter(
                    business_id=request.business.id,
                    purchase=purchase,
                    payment__reversal__isnull=True,
                ).aggregate(total=Sum("amount"))["total"]
                or Decimal("0.00")
            )

            outstanding = max(
                purchase.net_amount - paid,
                Decimal("0.00"),
            )

            if outstanding <= Decimal("0.00"):
                continue

            allocation = min(remaining, outstanding)

            allocations.append({
                "purchase_id": str(purchase.pk),
                "amount": allocation,
            })
            remaining -= allocation

        try:
            payment = record_supplier_payment(
                business_id=request.business.id,
                supplier_id=supplier.pk,
                payment_number=data["payment_number"],
                paid_at=data["paid_at"],
                amount=data["amount"],
                payment_method=data["payment_method"],
                reference_number=data["reference_number"],
                notes=data["notes"],
                allocations=allocations,
                user=request.user,
            )
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None)
            if detail is None:
                detail = exc.messages
            raise ValidationError(detail)

        return Response(
            SupplierPaymentReadSerializer(payment).data,
            status=status.HTTP_201_CREATED,
        )
