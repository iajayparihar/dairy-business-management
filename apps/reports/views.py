"""Stock reports. OWNER/MANAGER only (purchase volumes and cost data)."""
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.businesses.permissions import IsBusinessMember
from apps.purchases.milk_queries import _date_param
import uuid

from django.shortcuts import get_object_or_404

from apps.common.pagination import StandardPagination
from apps.customers.models import Customer
from apps.reports import (
    closing_stock, dashboard, exports, milk_balance, profit, tables,
)
from apps.suppliers.models import Supplier
from apps.reports.common import (
    ExportThrottle, can_see_salary, parse_as_of, parse_range, reject_unknown, today,
)
from apps.sales.views import MANAGE, fail


class ReportView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessMember]
    throttle_classes = [ExportThrottle]

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


class DailySummaryView(ReportView):
    def get(self, request, business_id):
        params = request.query_params
        try:
            reject_unknown(params, {"date", "export"})
            export = _choice(params, "export", ("CSV", "PDF"))
            day = _date_param(params, "date") or today(request.business)
            body = dashboard.daily_summary(request.business, day)
            if export:
                return _export_table(
                    tables.daily_table(body), request.business,
                    {"date": day.isoformat()}, export,
                )
        except DjangoValidationError as exc:
            return fail(exc)
        return Response(body)


class ProfitView(ReportView):
    MAX_DAYS = 366

    def get(self, request, business_id):
        params = request.query_params
        try:
            reject_unknown(params, {"period", "from", "to", "export"})
            export = _choice(params, "export", ("CSV", "PDF"))
            period = params.get("period", "month").lower()
            if period not in ("week", "month"):
                raise DjangoValidationError({"period": "Use week or month."})
            start, end, _, _ = parse_range(
                params, request.business, max_days=self.MAX_DAYS, default_days=90
            )
            body = profit.profit(
                request.business, period, start, end,
                include_salary=can_see_salary(request),
            )
            if export:
                return _export_table(
                    tables.profit_table(body), request.business,
                    {"period": period, "from": body["from"], "to": body["to"]}, export,
                )
        except DjangoValidationError as exc:
            return fail(exc)
        return Response(body)


def _export_table(table, business, params, export):
    if export == "CSV":
        return exports.csv_response(table, business, params)
    from apps.reports import pdf
    return pdf.pdf_response(table, business, params)


def _bool(params, name):
    raw = params.get(name, "false").lower()
    if raw not in ("true", "false"):
        raise DjangoValidationError({name: "Use true or false."})
    return raw == "true"


def _uuid(params, name):
    raw = params.get(name)
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise DjangoValidationError({name: "Not a valid id."})


def _choice(params, name, choices, default=None):
    raw = params.get(name)
    if not raw:
        return default
    for choice in choices:
        if raw.lower() == choice.lower():
            return choice
    raise DjangoValidationError({name: f"Use one of: {', '.join(choices)}."})


class TableReportView(ReportView):
    """
    One table report. Subclasses set `allowed` (extra query params) and
    implement build(request, params) -> table. `export=csv` returns the same
    table as a file (all rows); otherwise rows are paginated when
    `paginate` is set. Unknown query params are rejected with 400.
    """

    allowed = set()
    paginate = False

    def build(self, request, params):
        raise NotImplementedError

    def get(self, request, business_id):
        params = request.query_params
        try:
            reject_unknown(params, set(self.allowed) | {"export", "page", "page_size"})
            export = _choice(params, "export", ("CSV", "PDF"))
            table = self.build(request, params)
        except DjangoValidationError as exc:
            return fail(exc)
        echo = {k: v for k, v in params.items() if k not in ("page", "page_size", "export")}
        if export == "CSV":
            return exports.csv_response(table, request.business, echo)
        if export == "PDF":
            from apps.reports import pdf
            try:
                return pdf.pdf_response(table, request.business, echo)
            except DjangoValidationError as exc:
                return fail(exc)
        if not self.paginate:
            return Response(exports.render_json(table, request.business, echo))
        paginator = StandardPagination()
        page = paginator.paginate_queryset(table["rows"], request)
        return Response(exports.render_json(
            table, request.business, echo, rows=page,
            extra={"pagination": {
                "count": paginator.page.paginator.count,
                "page": paginator.page.number,
                "page_size": paginator.get_page_size(request),
                "num_pages": paginator.page.paginator.num_pages,
            }},
        ))


class SupplierBalancesView(TableReportView):
    allowed = {"as_of", "only_due"}
    paginate = True

    def build(self, request, params):
        return tables.supplier_balances(
            request.business, parse_as_of(params, request.business),
            only_due=_bool(params, "only_due"),
        )


class SupplierLedgerView(TableReportView):
    allowed = {"supplier_id", "from", "to"}
    paginate = True

    def build(self, request, params):
        supplier_id = _uuid(params, "supplier_id")
        if supplier_id is None:
            raise DjangoValidationError({"supplier_id": "Required."})
        start, end, _, _ = parse_range(
            params, request.business, max_days=366, default_days=30
        )
        supplier = get_object_or_404(
            Supplier.all_objects.filter(business=request.business), pk=supplier_id
        )
        return tables.supplier_ledger(request.business, supplier, start, end)


class CustomerOutstandingView(TableReportView):
    allowed = {"as_of", "customer_type", "only_due", "ordering"}
    paginate = True

    def build(self, request, params):
        return tables.customer_outstanding(
            request.business, parse_as_of(params, request.business),
            customer_type=_choice(params, "customer_type", Customer.CustomerType.values),
            only_due=_bool(params, "only_due"),
            ordering=_choice(
                params, "ordering",
                ("outstanding", "-outstanding", "name", "-name", "code", "-code"),
                default="-outstanding",
            ),
        )


class StockReportView(TableReportView):
    allowed = {"from", "to", "product_id", "only_flagged"}
    paginate = True

    def build(self, request, params):
        _, _, start, end = parse_range(
            params, request.business, max_days=92, default_days=31
        )
        return tables.stock_report(
            request.business, start, end, product_id=_uuid(params, "product_id"),
            only_flagged=_bool(params, "only_flagged"),
        )


class ProductSalesView(TableReportView):
    allowed = {"from", "to", "product_id", "group_by"}
    paginate = True

    def build(self, request, params):
        _, _, start, end = parse_range(
            params, request.business, max_days=92, default_days=31
        )
        return tables.product_sales(
            request.business, start, end, product_id=_uuid(params, "product_id"),
            group_by=_choice(params, "group_by", ("product", "day"), default="product"),
        )


class ExpenseReportView(TableReportView):
    allowed = {"from", "to", "category_id", "payment_method", "group_by"}
    paginate = True

    def build(self, request, params):
        start, end, _, _ = parse_range(
            params, request.business, max_days=92, default_days=31
        )
        from apps.expenses.models import PaymentMethod
        return tables.expense_report(
            request.business, start, end, category_id=_uuid(params, "category_id"),
            payment_method=_choice(params, "payment_method", PaymentMethod.values),
            group_by=_choice(params, "group_by", ("category", "day")),
        )
