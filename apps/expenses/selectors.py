"""
Employee money. `employee_position` / `employee_events` are the only places
an employee balance is summed.

balance = salary + bonus - deductions - payments   (cancelled rows excluded)
positive = owed to the employee, negative = advance already paid.
"""
from calendar import monthrange
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.db.models import Q, Sum

from apps.expenses.models import SalaryAccrual, SalaryPayment, Status

ZERO = Decimal("0.00")
K = SalaryAccrual.Kind


def days_in(month):
    return monthrange(month.year, month.month)[1]


def prorated_salary(monthly_salary, joined_on, left_on, month):
    """
    Salary for `month` by calendar days employed (join / leave day counted).
    Returns (amount, days_worked, days_in_month).
    """
    dim = days_in(month)
    first = max(joined_on, month)
    last_day = month.replace(day=dim)
    last = min(left_on, last_day) if left_on else last_day
    days = max((last - first).days + 1, 0)
    if days >= dim:
        return monthly_salary, dim, dim
    amount = (monthly_salary * days / dim).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return amount, days, dim


def _signed(accrual_kind, amount):
    return -amount if accrual_kind == K.DEDUCTION else amount


def _sums(accruals, payments):
    def total(kind):
        return accruals.filter(kind=kind).aggregate(t=Sum("amount"))["t"] or ZERO

    salary, bonus, deductions = total(K.SALARY), total(K.BONUS), total(K.DEDUCTION)
    paid = payments.aggregate(t=Sum("amount"))["t"] or ZERO
    return {
        "salary": salary, "bonus": bonus, "deductions": deductions, "paid": paid,
        "balance": salary + bonus - deductions - paid,
    }


def employee_position(employee, as_of=None):
    """Live figures, or the figures as they stood at `as_of` (an aware datetime)."""
    if as_of is None:
        position = _sums(
            SalaryAccrual.objects.filter(employee=employee, status=Status.POSTED),
            SalaryPayment.objects.filter(employee=employee, status=Status.POSTED),
        )
    else:
        position = {"salary": ZERO, "bonus": ZERO, "deductions": ZERO, "paid": ZERO}
        for e in employee_events(employee, end=as_of):
            key = {"SALARY": "salary", "BONUS": "bonus", "DEDUCTION": "deductions",
                   "PAYMENT": "paid"}.get(e["type"])
            # A cancel row undoes the figure it belongs to.
            if key:
                position[key] += e["amount"]
            else:
                original = e["type"].removesuffix("_CANCEL")
                key = {"SALARY": "salary", "BONUS": "bonus", "DEDUCTION": "deductions",
                       "PAYMENT": "paid"}[original]
                position[key] -= e["amount"]
        position["balance"] = (
            position["salary"] + position["bonus"] - position["deductions"]
            - position["paid"]
        )
    position["is_advance"] = position["balance"] < 0
    return position


def employee_events(employee, start=None, end=None):
    """
    Ledger events in time order, each with its effect on the balance
    (`delta`). An accrual counts from the first day of its month, a payment
    from `paid_at`, and every cancellation from the moment it was made.
    `start` is inclusive, `end` exclusive.
    """
    tz = ZoneInfo(employee.business.timezone)
    events = []
    for a in SalaryAccrual.objects.filter(employee=employee):
        at = datetime.combine(a.month, time.min, tzinfo=tz)
        events.append(_event(a, at, a.kind, a.amount, _signed(a.kind, a.amount)))
        if a.status == Status.CANCELLED:
            events.append(_event(
                a, a.cancelled_at, f"{a.kind}_CANCEL", a.amount,
                -_signed(a.kind, a.amount),
            ))
    for p in SalaryPayment.objects.filter(employee=employee):
        events.append(_event(p, p.paid_at, "PAYMENT", p.amount, -p.amount))
        if p.status == Status.CANCELLED:
            events.append(_event(p, p.cancelled_at, "PAYMENT_CANCEL", p.amount, p.amount))
    events = [
        e for e in events
        if (start is None or e["at"] >= start) and (end is None or e["at"] < end)
    ]
    events.sort(key=lambda e: (e["at"], e["row"].created_at, e["type"].endswith("_CANCEL")))
    return events


def _event(row, at, kind, amount, delta):
    return {"at": at, "type": kind, "amount": amount, "delta": delta, "row": row}


def employee_statement(employee, start_date, end_date):
    """Events of the local days start..end (inclusive) with a running balance."""
    tz = ZoneInfo(employee.business.timezone)
    start = datetime.combine(start_date, time.min, tzinfo=tz)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=tz)
    opening = employee_position(employee, as_of=start)["balance"]
    running = opening
    rows = []
    for e in employee_events(employee, start=start, end=end):
        running += e["delta"]
        rows.append({**e, "balance": running})
    return {"opening_balance": opening, "rows": rows, "closing_balance": running}


def employee_balances(business_id):
    """{employee_id: balance} for every employee with ledger rows, in 2 queries."""
    out = {}
    accruals = (
        SalaryAccrual.objects.filter(business_id=business_id, status=Status.POSTED)
        .values("employee_id")
        .annotate(
            plus=Sum("amount", filter=~Q(kind=K.DEDUCTION)),
            minus=Sum("amount", filter=Q(kind=K.DEDUCTION)),
        )
    )
    for r in accruals:
        out[r["employee_id"]] = (r["plus"] or ZERO) - (r["minus"] or ZERO)
    payments = (
        SalaryPayment.objects.filter(business_id=business_id, status=Status.POSTED)
        .values("employee_id").annotate(t=Sum("amount"))
    )
    for r in payments:
        out[r["employee_id"]] = out.get(r["employee_id"], ZERO) - r["t"]
    return out


def salary_cost_by_month(business_id, start_month, end_month):
    """
    Phase 9 hook: {month: salary + bonus - deductions} for POSTED rows,
    months being first-of-month dates between the two (inclusive).
    """
    rows = (
        SalaryAccrual.objects.filter(
            business_id=business_id, status=Status.POSTED,
            month__gte=start_month, month__lte=end_month,
        )
        .values("month")
        .annotate(
            plus=Sum("amount", filter=~Q(kind=K.DEDUCTION)),
            minus=Sum("amount", filter=Q(kind=K.DEDUCTION)),
        )
    )
    return {r["month"]: (r["plus"] or ZERO) - (r["minus"] or ZERO) for r in rows}
