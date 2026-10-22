import re

from rest_framework import serializers

PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,29}$")


class RejectUnknownFieldsMixin:
    """A typo such as `phon` must be a 400, not a silently dropped field."""

    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = set(data.keys()) - set(self.fields)
            if unknown:
                raise serializers.ValidationError(
                    {k: "Unknown field." for k in sorted(unknown)}
                )
        return super().to_internal_value(data)


def clean_phone(value):
    """Digits with an optional leading +; spaces, dashes and brackets are removed."""
    raw = (value or "").strip()
    value = re.sub(r"[\s\-().]", "", raw)
    if raw and not PHONE_RE.match(value):
        raise serializers.ValidationError("Enter a valid phone number.")
    return value


def clean_code(value):
    """Codes end up in bill / payment numbers, so keep them to A-Z 0-9 _ - and upper-case."""
    value = (value or "").strip().upper()
    if not CODE_RE.match(value):
        raise serializers.ValidationError(
            "Use 1-30 letters, digits, '-' or '_' (no spaces)."
        )
    return value


def validate_party_code(*, model, business, field, value, instance=None, has_records=None):
    """
    Shared by customers and suppliers: format, per-business uniqueness (soft-deleted rows
    keep their code) and a freeze once the party has records, because the code is part of
    the numbers of their bills, payments and milk cans.
    """
    code = clean_code(value)
    clash = model.all_objects.filter(business=business, **{field: code})
    if instance is not None:
        if code == getattr(instance, field):
            return code
        if has_records is not None and has_records(instance):
            raise serializers.ValidationError(
                "The code cannot change once there are records (deliveries, sales, "
                "bills, purchases or payments) under it."
            )
        clash = clash.exclude(pk=instance.pk)
    if clash.exists():
        raise serializers.ValidationError("This code is already used.")
    return code


class StrictDateTimeField(serializers.DateTimeField):
    """Refuses a timestamp without an offset instead of guessing the server's time zone."""

    def enforce_timezone(self, value):
        if value.tzinfo is None:
            raise serializers.ValidationError(
                "Provide a timezone-aware timestamp, e.g. 2026-10-22T06:30:00+05:30."
            )
        return super().enforce_timezone(value)


def decimals_to_str(value):
    """
    Raw `Decimal`s in a `Response` are rendered as JSON floats by DRF (`240.0`). Money must
    be an exact string: 2 decimals unless the value carries more (quantities, 3 places).
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return f"{value:.2f}" if value.as_tuple().exponent >= -2 else str(value)
    if isinstance(value, dict):
        return {k: decimals_to_str(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [decimals_to_str(v) for v in value]
    return value
