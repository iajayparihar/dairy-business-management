"""
Expenses (electricity, rent, ...) and employee salary.

Everything money-like is append-only: a row is never edited or deleted, it is
cancelled with a reason (who, when) or corrected (cancel + replacement).
Salary has no allocations: an employee's balance is one running figure,
    balance = salary + bonus - deductions - payments
positive = owed to the employee, negative = advance already paid.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from apps.businesses.models import Business
from apps.payments.models import ImmutableLedgerManager

CANCEL_FIELDS = {
    "status", "cancelled_at", "cancelled_by", "cancel_reason",
}


def _uuid_pk():
    return models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)


def _money():
    return models.DecimalField(
        max_digits=14, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )


def _user_fk():
    return models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )


class PaymentMethod(models.TextChoices):
    # Same values as supplier / customer payments (Phase 9 cash vs online).
    CASH = "CASH", "Cash"
    UPI = "UPI", "UPI"
    BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"
    CHEQUE = "CHEQUE", "Cheque"
    OTHER = "OTHER", "Other"


class Status(models.TextChoices):
    POSTED = "POSTED", "Posted"
    CANCELLED = "CANCELLED", "Cancelled"


class CancellableLedgerRow(models.Model):
    """A posted row: only the cancel fields may change, and it never goes away."""

    objects = ImmutableLedgerManager()

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.POSTED
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = _user_fk()
    cancel_reason = models.TextField(blank=True, default="")
    created_by = _user_fk()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            fields = kwargs.get("update_fields")
            if not fields or set(fields) - CANCEL_FIELDS:
                raise ValueError("Posted rows are immutable. Cancel instead.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Posted rows cannot be deleted. Cancel instead.")


def _cancel_needs_reason(name):
    return models.CheckConstraint(
        condition=Q(status="POSTED")
        | (Q(status="CANCELLED") & ~Q(cancel_reason="")),
        name=name,
    )


class ExpenseCategory(models.Model):
    id = _uuid_pk()
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="expense_categories"
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "expenses_category"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                "business", Lower("name"), name="uniq_expense_category_name"
            ),
        ]

    def delete(self, *args, **kwargs):
        raise ValueError("Categories cannot be deleted. Deactivate instead.")

    def __str__(self):
        return self.name


class Expense(CancellableLedgerRow):
    id = _uuid_pk()
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="expenses"
    )
    category = models.ForeignKey(
        ExpenseCategory, on_delete=models.PROTECT, related_name="expenses"
    )
    # EXP-<YYMMDD of expense_date>-<seq>, server-generated, never reused.
    expense_number = models.CharField(max_length=40, editable=False)
    # Local calendar date in Business.timezone (a bill is a date, not an instant).
    expense_date = models.DateField()
    amount = _money()
    payment_method = models.CharField(
        max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    payee = models.CharField(max_length=200, blank=True, default="")
    description = models.TextField(blank=True, default="")
    reference_number = models.CharField(max_length=100, blank=True, default="")
    client_ref = models.CharField(max_length=64, null=True, blank=True)
    replaces = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="replaced_by",
    )

    class Meta:
        db_table = "expenses_expense"
        ordering = ["-expense_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "expense_number"], name="uniq_expense_number"
            ),
            models.UniqueConstraint(
                fields=["business", "client_ref"],
                condition=Q(client_ref__isnull=False),
                name="uniq_expense_client_ref",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="expense_amount_positive"
            ),
            _cancel_needs_reason("expense_cancel_needs_reason"),
        ]
        indexes = [
            models.Index(
                fields=["business", "expense_date"], name="exp_biz_date_idx"
            ),
            models.Index(
                fields=["business", "category", "expense_date"],
                name="exp_biz_cat_date_idx",
            ),
        ]

    def __str__(self):
        return f"{self.expense_number} - {self.amount}"


class Employee(models.Model):
    id = _uuid_pk()
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="employees"
    )
    # EMP-<n>, server-generated, never reused.
    code = models.CharField(max_length=30, editable=False)
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, blank=True, default="")
    designation = models.CharField(max_length=100, blank=True, default="")
    # Only read when a month is generated; accruals keep their own snapshot.
    monthly_salary = _money()
    joined_on = models.DateField()
    left_on = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "expenses_employee"
        ordering = ["name", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "code"], name="uniq_employee_code"
            ),
            models.CheckConstraint(
                condition=Q(monthly_salary__gt=0), name="employee_salary_positive"
            ),
            models.CheckConstraint(
                condition=Q(left_on__isnull=True) | Q(left_on__gte=models.F("joined_on")),
                name="employee_left_after_joined",
            ),
        ]

    def delete(self, *args, **kwargs):
        raise ValueError("Employees cannot be deleted. Deactivate instead.")

    def __str__(self):
        return f"{self.code} {self.name}"


class SalaryAccrual(CancellableLedgerRow):
    """What the employee earned (SALARY, BONUS) or lost (DEDUCTION) for a month."""

    class Kind(models.TextChoices):
        SALARY = "SALARY", "Salary"
        BONUS = "BONUS", "Bonus"
        DEDUCTION = "DEDUCTION", "Deduction"

    id = _uuid_pk()
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="salary_accruals"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="accruals"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    # First day of the month this row belongs to.
    month = models.DateField()
    amount = _money()
    # SALARY snapshot: days worked in the month / days in the month.
    days_worked = models.PositiveSmallIntegerField(null=True, blank=True)
    days_in_month = models.PositiveSmallIntegerField(null=True, blank=True)
    monthly_salary = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    reason = models.TextField(blank=True, default="")
    # Required for BONUS / DEDUCTION (retry safe); SALARY is idempotent by month.
    client_ref = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        db_table = "expenses_salary_accrual"
        ordering = ["-month", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "month"],
                condition=Q(kind="SALARY", status="POSTED"),
                name="uniq_live_salary_per_month",
            ),
            models.UniqueConstraint(
                fields=["business", "client_ref"],
                condition=Q(client_ref__isnull=False),
                name="uniq_accrual_client_ref",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="accrual_amount_positive"
            ),
            models.CheckConstraint(
                condition=Q(month__day=1), name="accrual_month_first_day"
            ),
            models.CheckConstraint(
                condition=Q(kind="SALARY") | ~Q(reason=""),
                name="accrual_adjustment_needs_reason",
            ),
            _cancel_needs_reason("accrual_cancel_needs_reason"),
        ]
        indexes = [
            models.Index(
                fields=["business", "employee", "month"], name="accr_biz_emp_month_idx"
            ),
        ]


class SalaryPayment(CancellableLedgerRow):
    """Money handed to an employee. More than is due simply becomes an advance."""

    id = _uuid_pk()
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="salary_payments"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="payments"
    )
    # SAL-<EMPLOYEE_CODE>-<YYMMDD>-<seq>, server-generated, never reused.
    payment_number = models.CharField(max_length=60, editable=False)
    paid_at = models.DateTimeField()
    amount = _money()
    payment_method = models.CharField(
        max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    reference_number = models.CharField(max_length=100, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    client_ref = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        db_table = "expenses_salary_payment"
        ordering = ["-paid_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "payment_number"],
                name="uniq_salary_payment_number",
            ),
            models.UniqueConstraint(
                fields=["business", "client_ref"],
                condition=Q(client_ref__isnull=False),
                name="uniq_salary_payment_client_ref",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="salary_payment_amount_positive"
            ),
            _cancel_needs_reason("salary_payment_cancel_needs_reason"),
        ]
        indexes = [
            models.Index(
                fields=["business", "employee", "paid_at"],
                name="salpay_biz_emp_paid_idx",
            ),
        ]
