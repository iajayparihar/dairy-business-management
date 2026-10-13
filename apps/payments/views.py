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