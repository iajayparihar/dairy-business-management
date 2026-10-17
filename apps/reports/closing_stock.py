"""
Month-end closing stock, valued at the owner-set cost price.

Quantities come from the stock ledger on business time (occurred_at,
Business.timezone), month = [first day, first day of next month). The cost
per unit is the PURCHASE-type ProductPrice effective at the last instant of
the month. A product with no cost price is listed as unvalued with its
quantity; nothing is guessed. Figures are live: a backdated correction
changes a past month (ARCHITECTURE section 13).
"""
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError

from apps.inventory.services import stock_balances
from apps.products.models import Product, ProductPrice
from apps.products.services import get_product_price

ZERO = Decimal("0")
CENT = Decimal("0.01")


def month_bounds(business, year, month):
    tz = ZoneInfo(business.timezone)
    start = datetime.combine(datetime(year, month, 1).date(), time.min, tzinfo=tz)
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, nxt


def previous_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _money(value):
    return f"{value.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def _qty(value):
    return f"{value:.3f}"


def _snapshot(business, products, year, month):
    """{product_id: {opening, closing, rate|None, value|None}} for one month."""
    start, nxt = month_bounds(business, year, month)
    opening = stock_balances(business_id=business.pk, before=start)
    closing = stock_balances(business_id=business.pk, before=nxt)
    last_instant = nxt - timedelta(microseconds=1)
    out = {}
    for p in products:
        o, c = opening.get(p.pk, ZERO), closing.get(p.pk, ZERO)
        if o == 0 and c == 0:
            continue
        try:
            rate = get_product_price(
                business_id=business.pk, product_id=p.pk,
                price_type=ProductPrice.PriceType.PURCHASE, at=last_instant,
            ).unit_price
        except ValidationError:
            rate = None
        out[p.pk] = {
            "opening": o, "closing": c, "rate": rate,
            "value": None if rate is None else (c * rate).quantize(CENT, ROUND_HALF_UP),
        }
    return out


def closing_stock(*, business, year, month, compare=True):
    products = list(
        Product.objects.filter(business=business, track_stock=True).order_by("name", "sku")
    )
    this = _snapshot(business, products, year, month)
    prev_ym = previous_month(year, month)
    prev = _snapshot(business, products, *prev_ym) if compare else {}

    items, unvalued = [], []
    total_value = prev_total = gain_loss = ZERO
    for p in products:
        row = this.get(p.pk)
        if row is None:
            continue
        item = {
            "product_id": str(p.pk), "sku": p.sku, "name": p.name, "unit": p.unit,
            "opening_qty": _qty(row["opening"]), "closing_qty": _qty(row["closing"]),
            "is_negative": row["closing"] < 0,
            "cost_rate": None if row["rate"] is None else _money(row["rate"]),
            "closing_value": None if row["value"] is None else _money(row["value"]),
        }
        if row["value"] is None:
            unvalued.append({"product_id": item["product_id"], "name": p.name,
                             "unit": p.unit, "closing_qty": item["closing_qty"]})
        else:
            total_value += row["value"]
        if compare:
            before = prev.get(p.pk)
            item["previous_closing_qty"] = _qty(before["closing"] if before else ZERO)
            item["qty_change"] = _qty(row["closing"] - (before["closing"] if before else ZERO))
            # No stock last month is worth nothing, whatever the rate was.
            before_value = ZERO if before is None else before["value"]
            both = row["value"] is not None and before_value is not None
            item["value_change"] = _money(row["value"] - before_value) if both else None
            if both:
                gain_loss += row["value"] - before_value
        items.append(item)

    if compare:
        prev_total = sum((v["value"] for v in prev.values() if v["value"] is not None), ZERO)

    body = {
        "month": f"{year:04d}-{month:02d}",
        "timezone": business.timezone,
        "items": items,
        "total_closing_value": _money(total_value),
        "unvalued": unvalued,
    }
    if compare:
        body["compare"] = {
            "previous_month": "%04d-%02d" % prev_ym,
            "previous_total_closing_value": _money(prev_total),
            # Only products valued in both months; the others are listed
            # under `unvalued` or have no previous value.
            "stock_gain_loss": _money(gain_loss),
        }
    return body
