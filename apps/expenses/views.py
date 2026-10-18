"""
Expenses API (OWNER/MANAGER) and salary API (OWNER only).
Unknown body fields are a 400; money is a JSON string.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.common.pagination import StandardPagination
from apps.expenses import payroll, selectors, services
from apps.expenses.models import (
    Employee, Expense, ExpenseCategory, SalaryAccrual, SalaryPayment, Status,
)
from apps.purchases.milk_queries import _choice_param, _date_param
from apps.sales.views import check_body, datetime_field, fail, text, uuid_field

MANAGE = [BusinessMembership.Role.OWNER, BusinessMembership.Role.MANAGER]
OWNER = [BusinessMembership.Role.OWNER]
MAX_DAYS = 366
EXPENSE_KEYS = {"client_ref", "category_id", "amount", "expense_date", "payment_method",
                "payee", "description", "reference_number"}
CORRECT_KEYS = {"reason", "category_id", "amount", "payment_method", "payee",
                "description", "reference_number"}


def money(value):
    return f"{value:.2f}"


def iso(value):
    return value.isoformat() if value else None


def date_field(data, key):
    value = data.get(key)
    if value is None:
        return None
    try:
        parsed = parse_date(value) if isinstance(value, str) else None
    except ValueError:  # well-formed but impossible, e.g. 31 Feb
        parsed = None
    if parsed is None:
        raise DjangoValidationError({key: "Use YYYY-MM-DD format."})
    return parsed


def month_field(data, key="month", *, required=True):
    """'YYYY-MM' -> first day of that month."""
    value = data.get(key)
    if value is None and not required:
        return None
    try:
        assert isinstance(value, str) and len(value) == 7 and value[4] == "-"
        year, month = int(value[:4]), int(value[5:])
        return date(year, month, 1)
    except (AssertionError, ValueError):
        raise DjangoValidationError({key: "Use YYYY-MM format."})


def window(params, business, default_days=30):
    """?from & ?to (inclusive local days), at most a year; default = last N days."""
    start, end = _date_param(params, "from"), _date_param(params, "to")
    if (start is None) != (end is None):
        raise DjangoValidationError({"from": "Send both from and to, or neither."})
    if start is None:
        end = services.local_today(business)
        start = end - timedelta(days=default_days - 1)
    if start > end:
        raise DjangoValidationError({"to": "Must be on or after from."})
    if (end - start).days >= MAX_DAYS:
        raise DjangoValidationError({"to": f"At most {MAX_DAYS} days."})
    return start, end


def cancel_audit(row):
    return {
        "status": row.status,
        "cancelled_at": iso(row.cancelled_at),
        "cancelled_by": str(row.cancelled_by_id) if row.cancelled_by_id else None,
        "cancel_reason": row.cancel_reason,
    }


# --------------------------------------------------------- serializers

def serialize_category(c):
    return {"id": str(c.pk), "name": c.name, "is_active": c.is_active}


def serialize_expense(e):
    replaced_by = getattr(e, "replaced_by", None)
    return {
        "id": str(e.pk),
        "expense_number": e.expense_number,
        "category_id": str(e.category_id),
        "category_name": e.category.name,
        "expense_date": e.expense_date.isoformat(),
        "amount": money(e.amount),
        "payment_method": e.payment_method,
        "payee": e.payee,
        "description": e.description,
        "reference_number": e.reference_number,
        "replaces": str(e.replaces_id) if e.replaces_id else None,
        "replaced_by": str(replaced_by.pk) if replaced_by else None,
        "created_at": iso(e.created_at),
        **cancel_audit(e),
    }


def serialize_employee(e, balance=Decimal("0.00")):
    return {
        "id": str(e.pk), "code": e.code, "name": e.name, "phone": e.phone,
        "designation": e.designation, "monthly_salary": money(e.monthly_salary),
        "joined_on": e.joined_on.isoformat(), "left_on": iso(e.left_on),
        "is_active": e.is_active,
        "balance": money(balance), "is_advance": balance < 0,
    }


def serialize_accrual(a):
    return {
        "id": str(a.pk),
        "employee_id": str(a.employee_id),
        "employee_code": a.employee.code,
        "kind": a.kind,
        "month": a.month.strftime("%Y-%m"),
        "amount": money(a.amount),
        "days_worked": a.days_worked,
        "days_in_month": a.days_in_month,
        "monthly_salary": money(a.monthly_salary) if a.monthly_salary is not None else None,
        "reason": a.reason,
        "created_at": iso(a.created_at),
        **cancel_audit(a),
    }


def serialize_payment(p):
    return {
        "id": str(p.pk),
        "employee_id": str(p.employee_id),
        "employee_code": p.employee.code,
        "payment_number": p.payment_number,
        "paid_at": iso(p.paid_at),
        "amount": money(p.amount),
        "payment_method": p.payment_method,
        "reference_number": p.reference_number,
        "notes": p.notes,
        "created_at": iso(p.created_at),
        **cancel_audit(p),
    }


def serialize_position(p):
    return {
        "salary": money(p["salary"]), "bonus": money(p["bonus"]),
        "deductions": money(p["deductions"]), "paid": money(p["paid"]),
        "balance": money(p["balance"]), "is_advance": p["is_advance"],
    }


def paged(view, rows, request, serialize):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(rows, request)
    return paginator.get_paginated_response([serialize(r) for r in page])


# ---------------------------------------------------------- base views

class ExpensesView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessMember]
    roles = MANAGE

    def get_permissions(self):
        self.required_business_roles = self.roles
        return super().get_permissions()


class SalaryView(ExpensesView):
    """Salary data is OWNER only, reads included."""

    roles = OWNER


# ----------------------------------------------------------- categories

class CategoryListCreateView(ExpensesView):
    def get(self, request, business_id):
        rows = ExpenseCategory.objects.filter(business_id=request.business.id)
        try:
            active = request.query_params.get("is_active", "").lower()
            if active in ("true", "false"):
                rows = rows.filter(is_active=active == "true")
        except DjangoValidationError as exc:
            return fail(exc)
        return paged(self, rows, request, serialize_category)

    def post(self, request, business_id):
        try:
            check_body(request.data, {"name"}, required=("name",))
            c = services.create_category(
                business_id=request.business.id, user=request.user,
                name=text(request.data, "name"))
        except DjangoValidationError as exc:
            return fail(exc)
        return Response(serialize_category(c), status=status.HTTP_201_CREATED)


class CategoryDetailView(ExpensesView):
    def get(self, request, business_id, category_id):
        c = get_object_or_404(
            ExpenseCategory.objects.filter(business_id=request.business.id), pk=category_id)
        return Response(serialize_category(c))

    def patch(self, request, business_id, category_id):
        try:
            data = request.data
            check_body(data, {"name", "is_active"})
            get_object_or_404(
                ExpenseCategory.objects.filter(business_id=request.business.id), pk=category_id)
            c = services.update_category(
                business_id=request.business.id, category_id=category_id,
                user=request.user,
                name=text(data, "name", None) if "name" in data else None,
                is_active=data.get("is_active"))
        except DjangoValidationError as exc:
            return fail(exc)
        return Response(serialize_category(c))


# ------------------------------------------------------------- expenses

def expense_queryset(business_id):
    return Expense.objects.filter(business_id=business_id).select_related(
        "category").select_related("replaced_by")


class ExpenseListCreateView(ExpensesView):
    def get(self, request, business_id):
        params = request.query_params
        rows = expense_queryset(request.business.id)
        try:
            start, end = _date_param(params, "from"), _date_param(params, "to")
            if start and end and start > end:
                raise DjangoValidationError({"to": "Must be on or after from."})
            if start:
                rows = rows.filter(expense_date__gte=start)
            if end:
                rows = rows.filter(expense_date__lte=end)
            if (cat := uuid_field(params, "category_id")):
                rows = rows.filter(category_id=cat)
            if (state := _choice_param(params, "status", Status.values)):
                rows = rows.filter(status=state)
            if (method := _choice_param(params, "payment_method", services.PaymentMethod.values)):
                rows = rows.filter(payment_method=method)
            if (term := params.get("search", "").strip()):
                rows = rows.filter(
                    Q(payee__icontains=term) | Q(description__icontains=term)
                    | Q(expense_number__icontains=term) | Q(reference_number__icontains=term))
        except DjangoValidationError as exc:
            return fail(exc)
        return paged(self, rows, request, serialize_expense)

    def post(self, request, business_id):
        try:
            data = request.data
            check_body(data, EXPENSE_KEYS, required=("client_ref", "category_id", "amount"))
            e = services.create_expense(
                business_id=request.business.id, user=request.user,
                client_ref=text(data, "client_ref"),
                category_id=uuid_field(data, "category_id"),
                amount=data["amount"],
                expense_date=date_field(data, "expense_date"),
                payment_method=text(data, "payment_method") or None,
                payee=text(data, "payee"), description=text(data, "description"),
                reference_number=text(data, "reference_number"))
        except DjangoValidationError as exc:
            return fail(exc)
        return self._one(request, e.pk, status.HTTP_201_CREATED)

    @staticmethod
    def _one(request, pk, code=status.HTTP_200_OK):
        e = get_object_or_404(expense_queryset(request.business.id), pk=pk)
        return Response(serialize_expense(e), status=code)


class ExpenseDetailView(ExpensesView):
    def get(self, request, business_id, expense_id):
        return ExpenseListCreateView._one(request, expense_id)


class ExpenseCancelView(ExpensesView):
    def post(self, request, business_id, expense_id):
        try:
            check_body(request.data, {"reason"}, required=("reason",))
            get_object_or_404(expense_queryset(request.business.id), pk=expense_id)
            services.cancel_expense(
                business_id=request.business.id, expense_id=expense_id,
                user=request.user, reason=text(request.data, "reason"))
        except DjangoValidationError as exc:
            return fail(exc)
        return ExpenseListCreateView._one(request, expense_id)


class ExpenseCorrectView(ExpensesView):
    def post(self, request, business_id, expense_id):
        try:
            data = request.data
            check_body(data, CORRECT_KEYS, required=("reason",))
            get_object_or_404(expense_queryset(request.business.id), pk=expense_id)
            changes = {k: data[k] for k in CORRECT_KEYS - {"reason"} if k in data}
            if "category_id" in changes:
                changes["category_id"] = uuid_field(data, "category_id")
            for k in ("payment_method", "payee", "description", "reference_number"):
                if k in changes:
                    changes[k] = text(data, k)
            new = services.correct_expense(
                business_id=request.business.id, expense_id=expense_id,
                user=request.user, reason=text(data, "reason"), **changes)
        except DjangoValidationError as exc:
            return fail(exc)
        return ExpenseListCreateView._one(request, new.pk, status.HTTP_201_CREATED)


class ExpenseSummaryView(ExpensesView):
    def get(self, request, business_id):
        try:
            start, end = window(request.query_params, request.business)
        except DjangoValidationError as exc:
            return fail(exc)
        rows = services.expense_summary(business_id=request.business.id, start=start, end=end)
        total = sum((r["total"] for r in rows), Decimal("0.00"))
        return Response({
            "from": start.isoformat(), "to": end.isoformat(),
            "total": money(total),
            "categories": [
                {"category_id": str(r["category_id"]), "category_name": r["category__name"],
                 "total": money(r["total"]), "count": r["count"]}
                for r in rows
            ],
        })


# ------------------------------------------------------------ employees

def employee_queryset(business_id):
    return Employee.objects.filter(business_id=business_id)


class EmployeeListCreateView(SalaryView):
    def get(self, request, business_id):
        rows = employee_queryset(request.business.id)
        params = request.query_params
        active = params.get("is_active", "").lower()
        if active in ("true", "false"):
            rows = rows.filter(is_active=active == "true")
        if (term := params.get("search", "").strip()):
            rows = rows.filter(Q(name__icontains=term) | Q(code__icontains=term))
        balances = selectors.employee_balances(request.business.id)
        return paged(self, rows, request,
                     lambda e: serialize_employee(e, balances.get(e.pk, Decimal("0.00"))))

    def post(self, request, business_id):
        try:
            data = request.data
            check_body(data, {"name", "monthly_salary", "joined_on", "phone", "designation"},
                       required=("name", "monthly_salary", "joined_on"))
            e = payroll.create_employee(
                business_id=request.business.id, user=request.user,
                name=text(data, "name"), monthly_salary=data["monthly_salary"],
                joined_on=date_field(data, "joined_on"),
                phone=text(data, "phone"), designation=text(data, "designation"))
        except DjangoValidationError as exc:
            return fail(exc)
        return Response(serialize_employee(e), status=status.HTTP_201_CREATED)


class EmployeeScoped(SalaryView):
    def employee(self, request, employee_id):
        return get_object_or_404(employee_queryset(request.business.id), pk=employee_id)


class EmployeeDetailView(EmployeeScoped):
    def get(self, request, business_id, employee_id):
        e = self.employee(request, employee_id)
        return Response(serialize_employee(e, selectors.employee_position(e)["balance"]))

    def patch(self, request, business_id, employee_id):
        try:
            data = request.data
            check_body(data, payroll.UPDATABLE)
            self.employee(request, employee_id)
            changes = {}
            for k in ("name", "phone", "designation"):
                if k in data:
                    changes[k] = text(data, k)
            if "monthly_salary" in data:
                changes["monthly_salary"] = data["monthly_salary"]
            if "is_active" in data:
                changes["is_active"] = data["is_active"]
            if "left_on" in data:
                changes["left_on"] = date_field(data, "left_on")
            e = payroll.update_employee(
                business_id=request.business.id, employee_id=employee_id,
                user=request.user, **changes)
        except DjangoValidationError as exc:
            return fail(exc)
        return Response(serialize_employee(e, selectors.employee_position(e)["balance"]))


class EmployeeBalanceView(EmployeeScoped):
    def get(self, request, business_id, employee_id):
        e = self.employee(request, employee_id)
        return Response({"employee_id": str(e.pk), "code": e.code, "name": e.name,
                         **serialize_position(selectors.employee_position(e))})


class EmployeeStatementView(EmployeeScoped):
    def get(self, request, business_id, employee_id):
        e = self.employee(request, employee_id)
        try:
            start, end = window(request.query_params, request.business)
        except DjangoValidationError as exc:
            return fail(exc)
        st = selectors.employee_statement(e, start, end)
        return Response({
            "employee_id": str(e.pk), "code": e.code, "name": e.name,
            "from": start.isoformat(), "to": end.isoformat(),
            "opening_balance": money(st["opening_balance"]),
            "closing_balance": money(st["closing_balance"]),
            "transactions": [
                {"at": iso(r["at"]), "type": r["type"], "amount": money(r["amount"]),
                 "effect": money(r["delta"]), "balance": money(r["balance"]),
                 "id": str(r["row"].pk),
                 "month": r["row"].month.strftime("%Y-%m") if hasattr(r["row"], "month") else None,
                 "reason": (r["row"].cancel_reason if r["type"].endswith("_CANCEL")
                            else getattr(r["row"], "reason", ""))}
                for r in st["rows"]
            ],
        })


# -------------------------------------------------------------- salary

def accrual_queryset(business_id):
    return SalaryAccrual.objects.filter(business_id=business_id).select_related("employee")


def payment_queryset(business_id):
    return SalaryPayment.objects.filter(business_id=business_id).select_related("employee")


class AccrualListView(SalaryView):
    def get(self, request, business_id):
        params = request.query_params
        rows = accrual_queryset(request.business.id)
        try:
            if (emp := uuid_field(params, "employee_id")):
                rows = rows.filter(employee_id=emp)
            if params.get("month"):
                rows = rows.filter(month=month_field(params))
            if (kind := _choice_param(params, "kind", SalaryAccrual.Kind.values)):
                rows = rows.filter(kind=kind)
            if (state := _choice_param(params, "status", Status.values)):
                rows = rows.filter(status=state)
        except DjangoValidationError as exc:
            return fail(exc)
        return paged(self, rows, request, serialize_accrual)


class GenerateSalaryView(SalaryView):
    def post(self, request, business_id):
        try:
            data = request.data
            check_body(data, {"month", "employee_ids"}, required=("month",))
            ids = data.get("employee_ids")
            if ids is not None and (not isinstance(ids, list) or not ids):
                raise DjangoValidationError({"employee_ids": "Send a non-empty list."})
            out = payroll.generate_salary(
                business_id=request.business.id, user=request.user,
                month=month_field(data), employee_ids=ids)
        except DjangoValidationError as exc:
            return fail(exc)
        return Response({
            "created": [serialize_accrual(a) for a in out["created"]],
            "skipped": [{"employee_id": str(s["employee"].pk), "code": s["employee"].code,
                         "name": s["employee"].name, "reason": s["reason"]}
                        for s in out["skipped"]],
        }, status=status.HTTP_201_CREATED if out["created"] else status.HTTP_200_OK)


class AccrualCancelView(SalaryView):
    def post(self, request, business_id, accrual_id):
        try:
            check_body(request.data, {"reason"}, required=("reason",))
            get_object_or_404(accrual_queryset(request.business.id), pk=accrual_id)
            payroll.cancel_accrual(
                business_id=request.business.id, accrual_id=accrual_id,
                user=request.user, reason=text(request.data, "reason"))
        except DjangoValidationError as exc:
            return fail(exc)
        a = get_object_or_404(accrual_queryset(request.business.id), pk=accrual_id)
        return Response(serialize_accrual(a))


class AdjustmentCreateView(SalaryView):
    def post(self, request, business_id, employee_id):
        try:
            data = request.data
            check_body(data, {"client_ref", "kind", "month", "amount", "reason"},
                       required=("client_ref", "kind", "month", "amount", "reason"))
            get_object_or_404(employee_queryset(request.business.id), pk=employee_id)
            a = payroll.record_adjustment(
                business_id=request.business.id, user=request.user,
                employee_id=employee_id, client_ref=text(data, "client_ref"),
                kind=text(data, "kind"), month=month_field(data),
                amount=data["amount"], reason=text(data, "reason"))
        except DjangoValidationError as exc:
            return fail(exc)
        a = get_object_or_404(accrual_queryset(request.business.id), pk=a.pk)
        return Response(serialize_accrual(a), status=status.HTTP_201_CREATED)


class EmployeePaymentsView(SalaryView):
    def get(self, request, business_id, employee_id):
        get_object_or_404(employee_queryset(request.business.id), pk=employee_id)
        rows = payment_queryset(request.business.id).filter(employee_id=employee_id)
        return paged(self, rows, request, serialize_payment)

    def post(self, request, business_id, employee_id):
        try:
            data = request.data
            check_body(data, {"client_ref", "amount", "payment_method", "paid_at",
                              "reference_number", "notes"},
                       required=("client_ref", "amount"))
            get_object_or_404(employee_queryset(request.business.id), pk=employee_id)
            p = payroll.create_salary_payment(
                business_id=request.business.id, user=request.user,
                employee_id=employee_id, client_ref=text(data, "client_ref"),
                amount=data["amount"],
                payment_method=text(data, "payment_method") or None,
                paid_at=datetime_field(data, "paid_at"),
                reference_number=text(data, "reference_number"),
                notes=text(data, "notes"))
        except DjangoValidationError as exc:
            return fail(exc)
        p = get_object_or_404(payment_queryset(request.business.id), pk=p.pk)
        return Response(serialize_payment(p), status=status.HTTP_201_CREATED)


class SalaryPaymentDetailView(SalaryView):
    def get(self, request, business_id, payment_id):
        p = get_object_or_404(payment_queryset(request.business.id), pk=payment_id)
        return Response(serialize_payment(p))


class SalaryPaymentCancelView(SalaryView):
    def post(self, request, business_id, payment_id):
        try:
            check_body(request.data, {"reason"}, required=("reason",))
            get_object_or_404(payment_queryset(request.business.id), pk=payment_id)
            payroll.cancel_salary_payment(
                business_id=request.business.id, payment_id=payment_id,
                user=request.user, reason=text(request.data, "reason"))
        except DjangoValidationError as exc:
            return fail(exc)
        p = get_object_or_404(payment_queryset(request.business.id), pk=payment_id)
        return Response(serialize_payment(p))
