"""
Employees, monthly salary, bonus / deduction and salary payments (OWNER only).

Lock order (no deadlocks, all leaves of the project-wide order):
    business (no_key: numbers, client_ref, month generation)
    -> employee (no_key) -> accrual / payment
Cancel does not need the business lock: employee -> row.
An overpayment is never blocked: it shows as an advance.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.money import to_money
from apps.businesses.services import require_business_membership
from apps.expenses.models import Employee, SalaryAccrual, SalaryPayment, Status
from apps.expenses.selectors import days_in, prorated_salary
from apps.expenses.services import (
    OWNER_ONLY, clean_client_ref, clean_method, clean_reason, clean_text,
    local_today, next_number,
)
from apps.payments.services import ClientRefConflict
from apps.sales.services import _lock_business

K = SalaryAccrual.Kind
CLOCK_SKEW = timedelta(minutes=5)
TEXT_LIMITS = {"name": 200, "phone": 20, "designation": 100}


def _owner(user, business_id):
    require_business_membership(user=user, business_id=business_id, allowed_roles=OWNER_ONLY)


def _month_start(value, business, *, field="month"):
    if not isinstance(value, date) or hasattr(value, "hour") or value.day != 1:
        raise ValidationError({field: "Use the first day of a month."})
    if value > local_today(business).replace(day=1):
        raise ValidationError({field: "A future month cannot be recorded."})
    return value


def _lock_employee(business_or_id, employee_id):
    try:
        return Employee.objects.select_for_update(no_key=True).get(
            pk=employee_id, business_id=getattr(business_or_id, "pk", business_or_id)
        )
    except (Employee.DoesNotExist, ValueError, ValidationError) as exc:
        raise ValidationError({"employee": "Employee not found."}) from exc


def _text(field, value, *, required=False):
    value = (value or "").strip()
    if required and not value:
        raise ValidationError({field: "This field is required."})
    if len(value) > TEXT_LIMITS[field]:
        raise ValidationError({field: f"At most {TEXT_LIMITS[field]} characters."})
    return value


def _date(field, value):
    if not isinstance(value, date) or hasattr(value, "hour"):
        raise ValidationError({field: "Use a calendar date."})
    return value


# ----------------------------------------------------------- employees

@transaction.atomic
def create_employee(*, business_id, user, name, monthly_salary, joined_on,
                    phone="", designation=""):
    _owner(user, business_id)
    name = _text("name", name, required=True)
    phone, designation = _text("phone", phone), _text("designation", designation)
    salary = to_money(monthly_salary, "monthly_salary")
    joined_on = _date("joined_on", joined_on)
    business = _lock_business(business_id)
    try:
        with transaction.atomic():
            return Employee.objects.create(
                business=business,
                code=next_number(Employee, "code", business, "EMP-"),
                name=name, phone=phone, designation=designation,
                monthly_salary=salary, joined_on=joined_on,
            )
    except IntegrityError as exc:
        raise ValidationError("Could not allocate an employee code. Please retry.") from exc


UPDATABLE = {"name", "phone", "designation", "monthly_salary", "is_active", "left_on"}


@transaction.atomic
def update_employee(*, business_id, employee_id, user, **changes):
    """Master data only. A new salary affects months generated afterwards."""
    _owner(user, business_id)
    unknown = set(changes) - UPDATABLE
    if unknown:
        raise ValidationError({k: "This field cannot be changed." for k in sorted(unknown)})
    business = _lock_business(business_id)
    emp = _lock_employee(business, employee_id)
    if "name" in changes:
        emp.name = _text("name", changes["name"], required=True)
    for field in ("phone", "designation"):
        if field in changes:
            setattr(emp, field, _text(field, changes[field]))
    if "monthly_salary" in changes:
        emp.monthly_salary = to_money(changes["monthly_salary"], "monthly_salary")
    if "is_active" in changes:
        if not isinstance(changes["is_active"], bool):
            raise ValidationError({"is_active": "Must be true or false."})
        emp.is_active = changes["is_active"]
    if "left_on" in changes:
        emp.left_on = None if changes["left_on"] is None else _date("left_on", changes["left_on"])
    if emp.left_on and emp.left_on < emp.joined_on:
        raise ValidationError({"left_on": "Cannot be before the joining date."})
    emp.save()
    return emp


# ------------------------------------------------------ salary accrual

def _skip_reason(emp, month):
    """Why this employee gets no salary for `month`, or None."""
    month_end = month.replace(day=days_in(month))
    if not emp.is_active and emp.left_on is None:
        return "Employee is inactive."
    if emp.joined_on > month_end:
        return "Employee had not joined yet."
    if emp.left_on and emp.left_on < month:
        return "Employee had already left."
    return None


@transaction.atomic
def generate_salary(*, business_id, user, month, employee_ids=None):
    """
    Create the month's SALARY row for every eligible employee (pro-rata by
    calendar days when they joined or left inside the month). Safe to repeat:
    employees that already have a live row are listed under `skipped`.
    """
    _owner(user, business_id)
    business = _lock_business(business_id)
    month = _month_start(month, business)
    employees = Employee.objects.filter(business=business).order_by("pk")
    if employee_ids is not None:
        wanted = {str(i) for i in employee_ids}
        employees = employees.filter(pk__in=wanted)
        if employees.count() != len(wanted):
            raise ValidationError({
                "employee_ids": "An employee was not found in this business."
            })
    created, skipped = [], []
    for emp in list(employees):
        emp = _lock_employee(business, emp.pk)
        reason = _skip_reason(emp, month)
        amount = days = dim = None
        if reason is None:
            amount, days, dim = prorated_salary(
                emp.monthly_salary, emp.joined_on, emp.left_on, month
            )
            if amount <= 0:
                reason = "No salary is due for this month."
        if reason is None and SalaryAccrual.objects.filter(
            employee=emp, month=month, kind=K.SALARY, status=Status.POSTED
        ).exists():
            reason = "Already generated."
        if reason:
            skipped.append({"employee": emp, "reason": reason})
            continue
        created.append(SalaryAccrual.objects.create(
            business=business, employee=emp, kind=K.SALARY, month=month,
            amount=amount, days_worked=days, days_in_month=dim,
            monthly_salary=emp.monthly_salary, created_by=user,
        ))
    return {"created": created, "skipped": skipped}


@transaction.atomic
def record_adjustment(*, business_id, user, employee_id, client_ref, kind, month,
                      amount, reason):
    """A BONUS (adds to what is owed) or DEDUCTION (reduces it) for a month."""
    _owner(user, business_id)
    if kind not in (K.BONUS, K.DEDUCTION):
        raise ValidationError({"kind": "Use BONUS or DEDUCTION."})
    client_ref = clean_client_ref(client_ref)
    amount = to_money(amount, "amount")
    reason = clean_reason(reason)
    business = _lock_business(business_id)
    month = _month_start(month, business)
    emp = _lock_employee(business, employee_id)

    replay = SalaryAccrual.objects.filter(business=business, client_ref=client_ref).first()
    if replay is not None:
        if not (
            replay.employee_id == emp.pk and replay.kind == kind
            and replay.month == month and replay.amount == amount
            and replay.reason == reason
        ):
            raise ClientRefConflict({
                "client_ref": "This reference was already used for a different "
                              "adjustment. Use a new reference."
            })
        return replay
    return SalaryAccrual.objects.create(
        business=business, employee=emp, kind=kind, month=month, amount=amount,
        reason=reason, client_ref=client_ref, created_by=user,
    )


def _cancel(row, user, reason):
    row.status = Status.CANCELLED
    row.cancelled_at = timezone.now()
    row.cancelled_by = user
    row.cancel_reason = reason
    row.save(update_fields=["status", "cancelled_at", "cancelled_by", "cancel_reason"])
    return row


def _cancel_row(model, label, *, business_id, row_id, user, reason):
    _owner(user, business_id)
    reason = clean_reason(reason)
    try:
        # The employee of a row never changes, so peeking is safe.
        employee_id = model.objects.filter(
            pk=row_id, business_id=business_id
        ).values_list("employee_id", flat=True).get()
    except (model.DoesNotExist, ValueError, ValidationError) as exc:
        raise ValidationError({label: "Not found."}) from exc
    _lock_employee(business_id, employee_id)
    row = model.objects.select_for_update().get(pk=row_id, business_id=business_id)
    if row.status == Status.CANCELLED:
        raise ValidationError("This entry is already cancelled.")
    return _cancel(row, user, reason)


@transaction.atomic
def cancel_accrual(*, business_id, accrual_id, user, reason):
    return _cancel_row(SalaryAccrual, "accrual", business_id=business_id,
                       row_id=accrual_id, user=user, reason=reason)


# ------------------------------------------------------------ payments

@transaction.atomic
def create_salary_payment(*, business_id, user, employee_id, client_ref, amount,
                          payment_method=None, paid_at=None, reference_number="",
                          notes=""):
    """Pay an employee (salary or advance; the balance goes negative past what is due)."""
    _owner(user, business_id)
    client_ref = clean_client_ref(client_ref)
    amount = to_money(amount, "amount")
    method = clean_method(payment_method)
    reference_number = clean_text("reference_number", reference_number)
    notes = clean_text("description", notes)
    if paid_at is not None and (
        not isinstance(paid_at, datetime) or not timezone.is_aware(paid_at)
    ):
        raise ValidationError(
            {"paid_at": "A timestamp with timezone information is required."}
        )

    business = _lock_business(business_id)
    emp = _lock_employee(business, employee_id)

    replay = SalaryPayment.objects.filter(business=business, client_ref=client_ref).first()
    if replay is not None:
        if not (
            replay.employee_id == emp.pk and replay.amount == amount
            and replay.payment_method == method
            and replay.reference_number == reference_number
            and replay.notes == notes
            and (paid_at is None or replay.paid_at == paid_at)
        ):
            raise ClientRefConflict({
                "client_ref": "This reference was already used for a different "
                              "payment. Use a new reference."
            })
        return replay

    at = paid_at or timezone.now()
    if at > timezone.now() + CLOCK_SKEW:
        raise ValidationError({"paid_at": "A payment cannot be dated in the future."})
    day = at.astimezone(ZoneInfo(business.timezone)).strftime("%y%m%d")
    prefix = f"SAL-{emp.code}-{day}-"
    try:
        with transaction.atomic():
            return SalaryPayment.objects.create(
                business=business, employee=emp,
                payment_number=next_number(SalaryPayment, "payment_number", business, prefix),
                paid_at=at, amount=amount, payment_method=method,
                reference_number=reference_number, notes=notes,
                client_ref=client_ref, created_by=user,
            )
    except IntegrityError as exc:
        raise ValidationError("Could not allocate a payment number. Please retry.") from exc


@transaction.atomic
def cancel_salary_payment(*, business_id, payment_id, user, reason):
    return _cancel_row(SalaryPayment, "payment", business_id=business_id,
                       row_id=payment_id, user=user, reason=reason)
