"""
Daily milk balance: where each day's milk went, from the stock ledger.

Source is the append-only StockMovement ledger of the mapped cow / buffalo
products on business time (occurred_at, Business.timezone), so
opening + in - out = closing always equals stock_balance at the end of the
day. A reversal counts against the bucket of the movement it reverses, so a
cancelled entry nets to zero on its own day (ARCHITECTURE section 13).
Milk bought before a product mapping existed is not in the ledger; it is
shown as `unposted_bought_litres` instead of being hidden.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import (
    BooleanField, Case, F, Q, Sum, Value, When,
)
from django.db.models.functions import Coalesce, TruncDate

from apps.inventory.models import BusinessMilkConfig, StockMovement
from apps.inventory.services import STOCK_IN_TYPES, stock_balances
from apps.purchases.milk_queries import local_day_bounds
from apps.purchases.models import MilkCollection

MT = StockMovement.MovementType
MILK_TYPES = ("COW", "BUFFALO")
ZERO = Decimal("0")

# bucket -> True when it adds stock (shown positive), False when it removes.
BUCKETS = {
    "bought": True, "produced": True, "adjust_in": True,
    "delivered": False, "sold_shop": False, "converted": False,
    "wastage": False, "adjust_out": False,
}
ORIGIN_BUCKET = {
    MT.PURCHASE_RECEIPT: "bought",
    MT.PRODUCTION_IN: "produced",
    MT.ADJUSTMENT_IN: "adjust_in",
    MT.DELIVERY: "delivered",
    MT.SALE: "sold_shop",
    MT.PRODUCTION_OUT: "converted",
    MT.WASTAGE: "wastage",
    MT.ADJUSTMENT_OUT: "adjust_out",
}
MAX_DAYS = 62


def _bucket(origin, has_purchase_line):
    if origin == MT.ADJUSTMENT_IN and has_purchase_line:
        return "bought"  # purchase corrections belong with purchases
    return ORIGIN_BUCKET[origin]


def _empty(opening):
    row = {name: ZERO for name in BUCKETS}
    row["opening"] = opening
    return row


def _finish(row, unposted):
    row["closing"] = row["opening"] + sum(
        (row[n] if up else -row[n] for n, up in BUCKETS.items()), ZERO
    )
    row["is_negative"] = row["closing"] < 0
    row["unposted_bought_litres"] = unposted
    return row


def _qty(value):
    return f"{value:.3f}"


def milk_balance(*, business, start, end):
    """Rows for every local day in [start, end] (inclusive dates)."""
    tz = ZoneInfo(business.timezone)
    start_at, end_at = local_day_bounds(business, start)[0], local_day_bounds(business, end)[1]

    config = (
        BusinessMilkConfig.objects.filter(business=business)
        .select_related("cow_product", "buffalo_product").first()
    )
    products = {
        "COW": getattr(config, "cow_product", None),
        "BUFFALO": getattr(config, "buffalo_product", None),
    }
    type_of = {p.pk: t for t, p in products.items() if p is not None}

    opening = {t: ZERO for t in MILK_TYPES}
    for pid, balance in stock_balances(business_id=business.pk, before=start_at).items():
        if pid in type_of:
            opening[type_of[pid]] = balance

    moves = defaultdict(lambda: defaultdict(lambda: ZERO))  # (day, type) -> bucket -> qty
    if type_of:
        rows = (
            StockMovement.objects.filter(
                business=business, product_id__in=type_of,
                occurred_at__gte=start_at, occurred_at__lt=end_at,
            )
            .annotate(
                day=TruncDate("occurred_at", tzinfo=tz),
                origin=Coalesce(F("reversal_of__movement_type"), F("movement_type")),
                has_pl=Case(
                    When(
                        Q(purchase_line__isnull=False)
                        | Q(reversal_of__purchase_line__isnull=False),
                        then=Value(True),
                    ),
                    default=Value(False), output_field=BooleanField(),
                ),
            )
            .values("day", "product_id", "movement_type", "origin", "has_pl")
            .annotate(total=Sum("quantity"))
        )
        for r in rows:
            sign = 1 if r["movement_type"] in STOCK_IN_TYPES else -1
            bucket = _bucket(r["origin"], r["has_pl"])
            # IN buckets show net stock added, OUT buckets net stock removed.
            moves[(r["day"], type_of[r["product_id"]])][bucket] += (
                sign * r["total"] if BUCKETS[bucket] else -sign * r["total"]
            )

    collected = defaultdict(lambda: ZERO)
    for r in (
        MilkCollection.objects.filter(
            business=business, status=MilkCollection.Status.RECORDED,
            collected_at__gte=start_at, collected_at__lt=end_at,
        )
        .annotate(day=TruncDate("collected_at", tzinfo=tz))
        .values("day", "milk_type").annotate(total=Sum("litres"))
    ):
        collected[(r["day"], r["milk_type"])] += r["total"]

    days, day = [], start
    while day <= end:
        rows = {}
        for t in MILK_TYPES:
            row = _empty(opening[t])
            for bucket, value in moves.get((day, t), {}).items():
                row[bucket] += value
            rows[t] = _finish(row, collected[(day, t)] - row["bought"])
            opening[t] = rows[t]["closing"]
        total = {k: rows["COW"][k] + rows["BUFFALO"][k]
                 for k in rows["COW"] if k != "is_negative"}
        total["is_negative"] = total["closing"] < 0
        days.append({"date": day.isoformat(), **rows, "TOTAL": total})
        day += timedelta(days=1)

    def fmt(row):
        return {k: (v if isinstance(v, bool) else _qty(v)) for k, v in row.items()}

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "timezone": business.timezone,
        "mapping": {
            t: {
                "mapped": products[t] is not None,
                "product_id": str(products[t].pk) if products[t] else None,
                "product_name": products[t].name if products[t] else None,
            }
            for t in MILK_TYPES
        },
        "days": [
            {"date": d["date"], **{k: fmt(d[k]) for k in (*MILK_TYPES, "TOTAL")}}
            for d in days
        ],
    }
