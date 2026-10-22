"""Read-side helpers for milk collections: filters and the daily summary."""
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db.models import Count, ExpressionWrapper, F, DecimalField, Sum
from django.utils.dateparse import parse_date

from apps.purchases.models import MilkCollection
from apps.suppliers.models import Supplier

CENT = Decimal("0.01")


def local_day_bounds(business, day):
    """Half-open [start, next_day_start) for a business-local calendar day."""
    tz = ZoneInfo(business.timezone)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def _date_param(params, name):
    raw = params.get(name)
    if not raw:
        return None
    try:
        parsed = parse_date(raw)  # well-formed but impossible dates raise
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValidationError({name: "Use YYYY-MM-DD format."})
    # Far-off dates overflow day arithmetic (9999-12-31 + 1 day) and crash with a 500.
    if not 2000 <= parsed.year <= 2100:
        raise ValidationError({name: "The year must be between 2000 and 2100."})
    return parsed


def _choice_param(params, name, choices):
    raw = params.get(name)
    if not raw:
        return None
    raw = raw.upper()
    if raw not in choices:
        raise ValidationError({
            name: f"Choose one of: {', '.join(sorted(choices))}."
        })
    return raw


def filter_milk_collections(queryset, params, business):
    """
    Apply validated query-string filters. Day filters use the business's
    local days, so `end_date` includes the whole of that day.
    """
    start = _date_param(params, "start_date")
    end = _date_param(params, "end_date")

    if start and end and start > end:
        raise ValidationError({"end_date": "Must be on or after start_date."})

    if start:
        queryset = queryset.filter(
            collected_at__gte=local_day_bounds(business, start)[0],
        )
    if end:
        queryset = queryset.filter(
            collected_at__lt=local_day_bounds(business, end)[1],
        )

    for name, choices in (
        ("shift", MilkCollection.Shift.values),
        ("milk_type", MilkCollection.MilkType.values),
        ("status", MilkCollection.Status.values),
    ):
        value = _choice_param(params, name, choices)
        if value:
            queryset = queryset.filter(**{name: value})

    return queryset


def milk_daily_summary(*, business, day, supplier_id=None):
    """
    Litres, cost and average fat for one business-local day.

    Cancelled entries are excluded. Average fat is litre-weighted
    (sum(litres x fat) / sum(litres)) over BUFFALO only: cow fat is not
    recorded, so it is None when there is no buffalo milk.
    """
    start, end = local_day_bounds(business, day)

    queryset = MilkCollection.objects.filter(
        business=business,
        status=MilkCollection.Status.RECORDED,
        collected_at__gte=start,
        collected_at__lt=end,
    )

    if supplier_id is not None:
        if not Supplier.objects.filter(
            pk=supplier_id, business=business, is_deleted=False,
        ).exists():
            raise Supplier.DoesNotExist
        queryset = queryset.filter(supplier_id=supplier_id)

    weighted_fat = ExpressionWrapper(
        F("litres") * F("fat"),
        output_field=DecimalField(max_digits=20, decimal_places=5),
    )
    rows = (
        queryset.values("milk_type", "shift")
        .annotate(
            entries=Count("pk"),
            total_litres=Sum("litres"),
            total_amount=Sum("amount"),
            fat_litres=Sum(weighted_fat),
        )
        .order_by("milk_type", "shift")
    )

    def average_fat(milk_type, fat_litres, litres):
        if milk_type != MilkCollection.MilkType.BUFFALO or not litres:
            return None
        return (fat_litres / litres).quantize(CENT)

    breakdown = []
    totals = {"entries": 0, "litres": Decimal("0"), "amount": Decimal("0")}
    buffalo_litres = buffalo_fat_litres = Decimal("0")

    for row in rows:
        breakdown.append({
            "milk_type": row["milk_type"],
            "shift": row["shift"],
            "entries": row["entries"],
            "litres": row["total_litres"],
            "amount": row["total_amount"],
            "average_fat": average_fat(
                row["milk_type"], row["fat_litres"], row["total_litres"],
            ),
        })
        totals["entries"] += row["entries"]
        totals["litres"] += row["total_litres"]
        totals["amount"] += row["total_amount"]
        if row["milk_type"] == MilkCollection.MilkType.BUFFALO:
            buffalo_litres += row["total_litres"]
            buffalo_fat_litres += row["fat_litres"]

    totals["average_fat"] = average_fat(
        MilkCollection.MilkType.BUFFALO, buffalo_fat_litres, buffalo_litres,
    )

    return {"date": day, "totals": totals, "breakdown": breakdown}
