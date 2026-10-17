"""Stock reports. OWNER/MANAGER only (purchase volumes and cost data)."""
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.businesses.permissions import IsBusinessMember
from apps.purchases.milk_queries import _date_param
from apps.reports import closing_stock, milk_balance
from apps.sales.views import MANAGE, fail


class ReportView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessMember]

    def get_permissions(self):
        self.required_business_roles = MANAGE
        return super().get_permissions()


class MilkBalanceView(ReportView):
    def get(self, request, business_id):
        params = request.query_params
        try:
            day, start, end = (_date_param(params, k) for k in ("date", "from", "to"))
            if day and (start or end):
                raise DjangoValidationError({"date": "Use either date or from and to."})
            if day:
                start = end = day
            if start is None or end is None:
                raise DjangoValidationError(
                    {"date": "Send date, or both from and to (YYYY-MM-DD)."}
                )
            if start > end:
                raise DjangoValidationError({"to": "Must be on or after from."})
            if end - start >= timedelta(days=milk_balance.MAX_DAYS):
                raise DjangoValidationError(
                    {"to": f"At most {milk_balance.MAX_DAYS} days per request."}
                )
            body = milk_balance.milk_balance(
                business=request.business, start=start, end=end
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return Response(body)


class ClosingStockView(ReportView):
    def get(self, request, business_id):
        params = request.query_params
        try:
            raw = params.get("month", "")
            try:
                year, month = (int(x) for x in raw.split("-"))
                assert 1 <= month <= 12 and 1 <= year <= 9998 and len(raw) == 7
            except (ValueError, AssertionError):
                raise DjangoValidationError({"month": "Use YYYY-MM format."})
            compare = params.get("compare", "true").lower() != "false"
            body = closing_stock.closing_stock(
                business=request.business, year=year, month=month, compare=compare
            )
        except DjangoValidationError as exc:
            return fail(exc)
        return Response(body)
