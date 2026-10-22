import re

from django.core.exceptions import ValidationError

_MOBILE = re.compile(r"^[6-9]\d{9}$")


def normalize_phone(value):
    """Indian mobile as +91XXXXXXXXXX; blank stays blank. Raises ValueError otherwise."""
    raw = (value or "").strip()
    if not raw:
        return ""
    s = re.sub(r"[\s\-().]", "", raw)
    plus = s.startswith("+")
    d = s[1:] if plus else s
    if not d.isascii() or not d.isdigit():
        raise ValueError(raw)
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif plus:
        raise ValueError(raw)  # ponytail: only +91 numbers until other countries are needed
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if not _MOBILE.match(d):
        raise ValueError(raw)
    return "+91" + d


def validate_phone(value):
    try:
        normalize_phone(value)
    except ValueError:
        raise ValidationError("Enter a valid 10-digit Indian mobile number.")
