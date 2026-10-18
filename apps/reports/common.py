"""Shared helpers for the Phase 9 reports (read-only)."""
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework.throttling import UserRateThrottle

from apps.purchases.milk_queries import _date_param, local_day_bounds

ZERO = Decimal("0.00")
ZERO_QTY = Decimal("0.000")

# Owner decision (2026-10-18): UPI, bank transfer and cheque are "online";
# OTHER (e.g. opening-balance advances) is shown on its own, never merged.
PAYMENT_BUCKET = {
    "CASH": "cash",
    "UPI": "online",
    "BANK_TRANSFER": "online",
    "CHEQUE": "online",
}
BUCKETS = ("cash", "online", "other")


def bucket_of(method):
    return PAYMENT_BUCKET.get(method, "other")


def money(value):
    return f"{(value or ZERO):.2f}"


def qty(value):
    return f"{(value or ZERO_QTY):.3f}"


def reject_unknown(params, allowed):
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise DjangoValidationError(
            {k: "Unknown query parameter." for k in unknown}
        )


def today(business):
    return timezone.now().astimezone(ZoneInfo(business.timezone)).date()


def parse_range(params, business, *, max_days, default_days=None):
    """
    from / to (inclusive local dates) -> (from_date, to_date, start, end);
    start/end is the half-open aware range [start, end) in Business.timezone.
    """
    start_date = _date_param(params, "from")
    end_date = _date_param(params, "to")
    if start_date is None and end_date is None and default_days:
        end_date = today(business)
        start_date = end_date - timedelta(days=default_days - 1)
    if start_date is None or end_date is None:
        raise DjangoValidationError(
            {"from": "Send both from and to (YYYY-MM-DD)."}
        )
    if start_date > end_date:
        raise DjangoValidationError({"to": "Must be on or after from."})
    if (end_date - start_date).days + 1 > max_days:
        raise DjangoValidationError(
            {"to": f"At most {max_days} days per request."}
        )
    return (
        start_date,
        end_date,
        local_day_bounds(business, start_date)[0],
        local_day_bounds(business, end_date)[1],
    )


def parse_as_of(params, business):
    """as_of (a local date) -> aware exclusive bound (end of that day); None = now."""
    day = _date_param(params, "as_of")
    return local_day_bounds(business, day)[1] if day else None


def can_see_salary(request):
    """Salary data is OWNER only (superusers have no membership)."""
    membership = getattr(request, "business_membership", None)
    return membership is None or membership.role == "OWNER"


class ExportThrottle(UserRateThrottle):
    """Rate-limits only the heavy part: requests that ask for an export."""

    scope = "reports_export"

    def allow_request(self, request, view):
        if "export" not in request.query_params:
            return True
        return super().allow_request(request, view)
