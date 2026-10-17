from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

# Money columns are Decimal(14, 2).
LIMIT = Decimal("1000000000000")


def to_money(value, field_name, *, signed=False, allow_zero=False):
    """
    Parse an amount with at most 2 decimals.

    By default positive only. signed=True accepts any non-zero amount;
    allow_zero=True accepts zero or more.
    """
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError({field_name: "Enter a valid amount."})

    if not amount.is_finite():
        raise ValidationError({field_name: "Enter a valid amount."})

    if signed:
        if amount == 0:
            raise ValidationError({field_name: "Amount must not be zero."})
    elif allow_zero:
        if amount < 0:
            raise ValidationError({field_name: "Amount must not be negative."})
    elif amount <= 0:
        raise ValidationError({field_name: "Amount must be positive."})

    if amount.as_tuple().exponent < -2:
        raise ValidationError({
            field_name: "Amount can have at most 2 decimal places."
        })

    if abs(amount) >= LIMIT:
        raise ValidationError({field_name: "Amount is too large."})

    return amount
