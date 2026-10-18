"""
Expenses and categories (OWNER/MANAGER).

Lock order (same as the rest of the project, no deadlocks):
    business (no_key: numbers, client_ref, categories) -> expense
"""
import re
from datetime import date
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.billing.money import to_money
from apps.businesses.models import BusinessMembership
from apps.businesses.services import require_business_membership
from apps.expenses.models import Expense, ExpenseCategory, PaymentMethod, Status
from apps.payments.services import ClientRefConflict
from apps.sales.services import _lock_business

MANAGE_ROLES = [BusinessMembership.Role.OWNER, BusinessMembership.Role.MANAGER]
OWNER_ONLY = [BusinessMembership.Role.OWNER]
MIN_REASON_LENGTH = 3
TEXT_LIMITS = {"payee": 200, "reference_number": 100, "description": 2000}
CORRECTABLE = {
    "category_id", "amount", "payment_method", "payee", "description",
    "reference_number",
}


# ------------------------------------------------------------- helpers

def local_today(business):
    return timezone.now().astimezone(ZoneInfo(business.timezone)).date()


def clean_reason(reason):
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_LENGTH:
        raise ValidationError({
            "reason": f"Give a reason of at least {MIN_REASON_LENGTH} characters."
        })
    return reason


def clean_client_ref(client_ref):
    client_ref = (client_ref or "").strip()
    if not client_ref or len(client_ref) > 64:
        raise ValidationError(
            {"client_ref": "A client_ref of 1 to 64 characters is required."}
        )
    return client_ref


def clean_text(field, value):
    value = (value or "").strip()
    if len(value) > TEXT_LIMITS[field]:
        raise ValidationError({field: f"At most {TEXT_LIMITS[field]} characters."})
    return value


def clean_method(value):
    value = value or PaymentMethod.CASH
    if value not in PaymentMethod.values:
        raise ValidationError({"payment_method": "Invalid payment method."})
    return value


def next_number(model, field, business, prefix):
    """Next `<prefix><n>` for this business; call only under the business lock."""
    pattern = re.compile(re.escape(prefix) + r"(\d+)")
    taken = model.objects.filter(
        business=business, **{f"{field}__startswith": prefix}
    ).values_list(field, flat=True)
    seqs = [int(m.group(1)) for n in taken if (m := pattern.fullmatch(n))]
    return f"{prefix}{max(seqs, default=0) + 1}"


# ---------------------------------------------------------- categories

def _clean_category_name(name):
    name = (name or "").strip()
    if not name or len(name) > 100:
        raise ValidationError({"name": "A name of 1 to 100 characters is required."})
    return name


def _name_taken(business, name, exclude_pk=None):
    rows = ExpenseCategory.objects.filter(business=business, name__iexact=name)
    return rows.exclude(pk=exclude_pk).exists()


@transaction.atomic
def create_category(*, business_id, user, name):
    require_business_membership(user=user, business_id=business_id, allowed_roles=MANAGE_ROLES)
    name = _clean_category_name(name)
    business = _lock_business(business_id)
    if _name_taken(business, name):
        raise ValidationError({"name": "This category already exists."})
    try:
        with transaction.atomic():
            return ExpenseCategory.objects.create(business=business, name=name)
    except IntegrityError as exc:
        raise ValidationError({"name": "This category already exists."}) from exc


@transaction.atomic
def update_category(*, business_id, category_id, user, name=None, is_active=None):
    require_business_membership(user=user, business_id=business_id, allowed_roles=MANAGE_ROLES)
    business = _lock_business(business_id)
    try:
        category = ExpenseCategory.objects.select_for_update().get(
            pk=category_id, business=business
        )
    except ExpenseCategory.DoesNotExist as exc:
        raise ValidationError({"category": "Category not found."}) from exc
    if name is not None:
        name = _clean_category_name(name)
        if _name_taken(business, name, exclude_pk=category.pk):
            raise ValidationError({"name": "This category already exists."})
        category.name = name
    if is_active is not None:
        if not isinstance(is_active, bool):
            raise ValidationError({"is_active": "Must be true or false."})
        category.is_active = is_active
    category.save()
    return category


def _active_category(business, category_id):
    try:
        return ExpenseCategory.objects.get(
            pk=category_id, business=business, is_active=True
        )
    except (ExpenseCategory.DoesNotExist, ValueError, ValidationError) as exc:
        raise ValidationError({
            "category_id": (
                "The category does not exist, is inactive, "
                "or belongs to another business."
            )
        }) from exc


# ------------------------------------------------------------ expenses

def _check_date(business, value):
    if value is None:
        return None
    if not isinstance(value, date) or hasattr(value, "hour"):
        raise ValidationError({"expense_date": "Use a calendar date."})
    if value > local_today(business):
        raise ValidationError(
            {"expense_date": "An expense cannot be dated in the future."}
        )
    return value


@transaction.atomic
def create_expense(
    *, business_id, user, client_ref, category_id, amount, expense_date=None,
    payment_method=None, payee="", description="", reference_number="",
):
    """
    Record an expense. Retrying the same client_ref returns the original;
    the same ref with different data raises ClientRefConflict.
    """
    require_business_membership(user=user, business_id=business_id, allowed_roles=MANAGE_ROLES)
    client_ref = clean_client_ref(client_ref)
    amount = to_money(amount, "amount")
    method = clean_method(payment_method)
    payee = clean_text("payee", payee)
    description = clean_text("description", description)
    reference_number = clean_text("reference_number", reference_number)

    business = _lock_business(business_id)
    explicit_date = _check_date(business, expense_date)

    replay = Expense.objects.filter(business=business, client_ref=client_ref).first()
    if replay is not None:
        same = (
            str(replay.category_id) == str(category_id)
            and replay.amount == amount
            and replay.payment_method == method
            and replay.payee == payee
            and replay.description == description
            and replay.reference_number == reference_number
            # A defaulted date (today) is not compared: a retry may be tomorrow.
            and (explicit_date is None or replay.expense_date == explicit_date)
        )
        if not same:
            raise ClientRefConflict({
                "client_ref": (
                    "This reference was already used for a different "
                    "expense. Use a new reference."
                )
            })
        return replay

    category = _active_category(business, category_id)
    day = explicit_date or local_today(business)
    return _insert(
        business, user, category, day, amount, method, payee, description,
        reference_number, client_ref=client_ref,
    )


def _insert(business, user, category, day, amount, method, payee, description,
            reference_number, *, client_ref=None, replaces=None):
    prefix = f"EXP-{day.strftime('%y%m%d')}-"
    try:
        with transaction.atomic():
            return Expense.objects.create(
                business=business,
                category=category,
                expense_number=next_number(Expense, "expense_number", business, prefix),
                expense_date=day,
                amount=amount,
                payment_method=method,
                payee=payee,
                description=description,
                reference_number=reference_number,
                client_ref=client_ref,
                replaces=replaces,
                created_by=user,
            )
    except IntegrityError as exc:
        raise ValidationError(
            "Could not allocate an expense number. Please retry."
        ) from exc


def _lock_expense(business, expense_id):
    try:
        return Expense.objects.select_for_update().get(pk=expense_id, business=business)
    except (Expense.DoesNotExist, ValueError, ValidationError) as exc:
        raise ValidationError({"expense": "Expense not found."}) from exc


def _cancel(expense, user, reason):
    expense.status = Status.CANCELLED
    expense.cancelled_at = timezone.now()
    expense.cancelled_by = user
    expense.cancel_reason = reason
    expense.save(update_fields=[
        "status", "cancelled_at", "cancelled_by", "cancel_reason",
    ])


@transaction.atomic
def cancel_expense(*, business_id, expense_id, user, reason):
    require_business_membership(user=user, business_id=business_id, allowed_roles=MANAGE_ROLES)
    reason = clean_reason(reason)
    business = _lock_business(business_id)
    expense = _lock_expense(business, expense_id)
    if expense.status == Status.CANCELLED:
        raise ValidationError("This expense is already cancelled.")
    _cancel(expense, user, reason)
    return expense


@transaction.atomic
def correct_expense(*, business_id, expense_id, user, reason, **changes):
    """Cancel the expense and post a replacement on the original date."""
    require_business_membership(user=user, business_id=business_id, allowed_roles=MANAGE_ROLES)
    reason = clean_reason(reason)
    unknown = set(changes) - CORRECTABLE
    if unknown:
        raise ValidationError({k: "This field cannot be corrected." for k in sorted(unknown)})
    if not changes:
        raise ValidationError("Send at least one field to correct.")

    business = _lock_business(business_id)
    old = _lock_expense(business, expense_id)
    if old.status == Status.CANCELLED:
        raise ValidationError("A cancelled expense cannot be corrected.")

    category = (
        _active_category(business, changes["category_id"])
        if "category_id" in changes else old.category
    )
    amount = to_money(changes["amount"], "amount") if "amount" in changes else old.amount
    method = clean_method(changes["payment_method"]) if "payment_method" in changes else old.payment_method
    texts = {
        f: clean_text(f, changes[f]) if f in changes else getattr(old, f)
        for f in TEXT_LIMITS
    }
    _cancel(old, user, f"Corrected: {reason}")
    return _insert(
        business, user, category, old.expense_date, amount, method,
        texts["payee"], texts["description"], texts["reference_number"],
        replaces=old,
    )


# ------------------------------------------------------------- reports

def expense_summary(*, business_id, start, end):
    """POSTED expenses between two local dates (inclusive), per category."""
    rows = (
        Expense.objects.filter(
            business_id=business_id, status=Status.POSTED,
            expense_date__gte=start, expense_date__lte=end,
        )
        .values("category_id", "category__name")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("category__name")
    )
    return list(rows)
