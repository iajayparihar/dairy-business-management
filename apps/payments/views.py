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

from django.db import transaction
from apps.businesses.models import BusinessMembership
from apps.payments.api_serializers import (
    PaymentAllocateSerializer,
    PaymentReverseSerializer,
    SupplierPaymentCreateSerializer,
    SupplierPaymentReadSerializer,
    serialize_allocation,
    serialize_reversal,
)
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocationReversal,
)
from apps.purchases.api_views import raise_api_validation_error
from apps.payments.selectors import get_payment_amounts, get_supplier_balance
from apps.payments.services import (
    ClientRefConflict,
    allocate_supplier_advance,
    record_supplier_payment,
    reverse_supplier_payment,
)

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
                auto_allocate=True,
                client_ref=data.get("client_ref"),
                user=request.user,
            )
        except ClientRefConflict as exc:
            return Response(
                exc.message_dict, status=status.HTTP_409_CONFLICT
            )
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None)
            if detail is None:
                detail = exc.messages
            raise ValidationError(detail)

        return Response(
            SupplierPaymentReadSerializer(payment).data,
            status=(
                status.HTTP_200_OK
                if payment.replayed
                else status.HTTP_201_CREATED
            ),
        )



class OwnerManagerPaymentView(APIView):
    """Payment detail/actions expose money data: OWNER/MANAGER only."""

    permission_classes = [IsAuthenticated, IsBusinessMember]

    def get_permissions(self):
        self.required_business_roles = [
            BusinessMembership.Role.OWNER,
            BusinessMembership.Role.MANAGER,
        ]
        return super().get_permissions()

    def get_payment(self, request, payment_id):
        # Tenant-scoped: another business's payment is simply a 404.
        return get_object_or_404(
            SupplierPayment.objects.filter(business_id=request.business.id),
            pk=payment_id,
        )

    def payment_detail(self, payment):
        reversals = list(payment.reversals.select_related("created_by"))
        unwinds_by_reversal = {}
        for unwind in (
            SupplierPaymentAllocationReversal.objects
            .filter(reversal__payment=payment)
            .select_related("allocation__purchase")
            .order_by("created_at", "pk")
        ):
            unwinds_by_reversal.setdefault(unwind.reversal_id, []).append(unwind)

        allocations = (
            payment.allocations.select_related("purchase")
            .prefetch_related("unwinds")
            .order_by("created_at", "pk")
        )

        return {
            **SupplierPaymentReadSerializer(payment).data,
            "amounts": get_payment_amounts(payment),
            "allocations": [serialize_allocation(a) for a in allocations],
            "reversals": [
                serialize_reversal(r, unwinds_by_reversal.get(r.id, []))
                for r in reversals
            ],
        }


class SupplierPaymentDetailAPIView(OwnerManagerPaymentView):
    def get(self, request, business_id, payment_id):
        payment = self.get_payment(request, payment_id)
        return Response(self.payment_detail(payment))


class SupplierPaymentReverseAPIView(OwnerManagerPaymentView):
    """
    POST {amount?, reason, client_ref?}
    201 new reversal, 200 identical client_ref replay, 409 client_ref reused
    for different data, 400 invalid (excessive amount, already fully
    reversed, missing reason). Response: the reversal plus the payment detail.
    """

    def post(self, request, business_id, payment_id):
        payment = self.get_payment(request, payment_id)

        serializer = PaymentReverseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            reversal = reverse_supplier_payment(
                payment_id=payment.pk,
                business_id=request.business.id,
                amount=data.get("amount"),
                reason=data["reason"],
                client_ref=data.get("client_ref"),
                user=request.user,
            )
        except ClientRefConflict as exc:
            return Response(exc.message_dict, status=status.HTTP_409_CONFLICT)
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)

        payment.refresh_from_db()
        return Response(
            {
                "reversal": serialize_reversal(reversal, reversal.unwound),
                "payment": self.payment_detail(payment),
            },
            status=(
                status.HTTP_200_OK
                if reversal.replayed
                else status.HTTP_201_CREATED
            ),
        )


class SupplierPaymentAllocateAPIView(OwnerManagerPaymentView):
    """
    POST {purchase_id, amount}
    201 allocated, 200 identical allocation already exists (retry),
    400 invalid (exceeds remaining advance or purchase outstanding,
    other supplier, not received, already allocated with a different
    amount). Response: the allocation plus the payment detail.
    """

    def post(self, request, business_id, payment_id):
        payment = self.get_payment(request, payment_id)

        serializer = PaymentAllocateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            allocation = allocate_supplier_advance(
                payment_id=payment.pk,
                business_id=request.business.id,
                purchase_id=data["purchase_id"],
                amount=data["amount"],
                user=request.user,
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)

        return Response(
            {
                "allocation": serialize_allocation(allocation),
                "payment": self.payment_detail(payment),
            },
            status=(
                status.HTTP_200_OK
                if allocation.replayed
                else status.HTTP_201_CREATED
            ),
        )


class SupplierBalanceAPIView(OwnerManagerPaymentView):
    """GET: outstanding (not clamped, negative = refund due) and advance."""

    def get(self, request, business_id, supplier_id):
        supplier = get_object_or_404(
            Supplier.objects.filter(
                business_id=request.business.id, is_deleted=False
            ),
            pk=supplier_id,
        )
        return Response({
            "supplier_id": str(supplier.pk),
            **get_supplier_balance(
                business_id=request.business.id, supplier_id=supplier.pk
            ),
        })
