"""
Weekly / monthly operational profit ESTIMATE (not accounting profit).

  simple          = sales - purchases - expenses - salary
  stock_adjusted  = sales - (purchases - inventory_change) - expenses - salary

purchases = goods bought in the bucket (not cost of goods sold), so the
stock-adjusted line removes the part still sitting in stock. inventory_change
is the value of tracked stock at the end minus at the start of the bucket,
at the owner-set PURCHASE price; a product with no price is `unvalued` and
left out, never valued at zero.

Salary is a monthly figure (it belongs to the month, not the payment day),
so it is included in month buckets and NOT in week buckets (flagged).
Collections and balances never enter profit.
"""
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth, TruncWeek
from django.utils import timezone
from zoneinfo import ZoneInfo

from apps.deliveries.models import Delivery
from apps.expenses.models import Expense, Status as ExpenseStatus
from apps.expenses.selectors import salary_cost_by_month
from apps.inventory.services import stock_balances
from apps.products.models import Product, ProductPrice
from apps.purchases.milk_queries import local_day_bounds
from apps.purchases.models import (
    MilkCollection,
    PurchaseAdjustment,
    SupplierPurchase,
)
from apps.reports.common import ZERO, money
from apps.sales.models import Sale

CENT = Decimal("0.01")


def _month_floor(d):
    return d.replace(day=1)


def _month_next(d):
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def snap_range(period, start_date, end_date):
    """Widen [from, to] to whole ISO weeks (Mon-Sun) or calendar months."""
    if period == "week":
        s = start_date - timedelta(days=start_date.weekday())
        e = end_date + timedelta(days=6 - end_date.weekday())
    else:
        s = _month_floor(start_date)
        e = _month_next(end_date) - timedelta(days=1)
    return s, e


def bucket_starts(period, start_date, end_date):
    out, d = [], start_date
    while d <= end_date:
        out.append(d)
        d = d + timedelta(days=7) if period == "week" else _month_next(d)
    return out


def _grouped_by_bucket(qs, field, value, period, tz=None):
    trunc = TruncWeek if period == "week" else TruncMonth
    kw = {"tzinfo": tz} if tz is not None else {}
    rows = (
        qs.order_by().annotate(b=trunc(field, **kw)).values("b").annotate(t=Sum(value))
    )
    out = {}
    for r in rows:
        b = r["b"].date() if isinstance(r["b"], datetime) else r["b"]
        out[b] = out.get(b, ZERO) + (r["t"] or ZERO)
    return out


def _price_book(business_id):
    """{product_id: [(from, to|None, unit_price)]} for PURCHASE prices."""
    book = {}
    rows = ProductPrice.objects.filter(
        business_id=business_id, price_type=ProductPrice.PriceType.PURCHASE
    ).values_list("product_id", "effective_from", "effective_to", "unit_price")
    for pid, f, t, price in rows:
        book.setdefault(pid, []).append((f, t, price))
    return book


def _rate(book, product_id, at):
    for f, t, price in book.get(product_id, ()):
        if f <= at and (t is None or at < t):
            return price
    return None


def stock_value_at(business_id, at, book, tracked):
    """(value of valued tracked stock, [unvalued product ids]) just before `at`."""
    balances = stock_balances(business_id=business_id, before=at)
    last = at - timedelta(microseconds=1)
    total, unvalued = ZERO, []
    for pid, bal in balances.items():
        if pid not in tracked or bal == 0:
            continue
        rate = _rate(book, pid, last)
        if rate is None:
            unvalued.append(pid)
        else:
            total += (bal * rate).quantize(CENT, ROUND_HALF_UP)
    return total, unvalued


def profit(business, period, from_date, to_date, include_salary=True):
    bid = business.pk
    tz = ZoneInfo(business.timezone)
    s_date, e_date = snap_range(period, from_date, to_date)
    start = local_day_bounds(business, s_date)[0]
    end = local_day_bounds(business, e_date)[1]
    starts = bucket_starts(period, s_date, e_date)

    def rng(model_qs, field):
        return model_qs.filter(**{f"{field}__gte": start, f"{field}__lt": end})

    deliveries = _grouped_by_bucket(
        rng(Delivery.objects.filter(business_id=bid, status=Delivery.Status.DELIVERED), "delivered_at"),
        "delivered_at", "line_total", period, tz)
    shop = _grouped_by_bucket(
        rng(Sale.objects.filter(business_id=bid, status=Sale.Status.COMPLETED), "sold_at"),
        "sold_at", "total_amount", period, tz)
    milk = _grouped_by_bucket(
        rng(MilkCollection.objects.filter(business_id=bid, status=MilkCollection.Status.RECORDED), "collected_at"),
        "collected_at", "amount", period, tz)
    received = SupplierPurchase.objects.filter(
        business_id=bid, status=SupplierPurchase.Status.RECEIVED, is_deleted=False)
    other = _grouped_by_bucket(
        rng(received.filter(milk_collection__isnull=True), "purchased_at"),
        "purchased_at", "total_amount", period, tz)
    adjust = _grouped_by_bucket(
        rng(PurchaseAdjustment.objects.filter(business_id=bid, purchase__in=received), "occurred_at"),
        "occurred_at", "amount", period, tz)
    expenses = _grouped_by_bucket(
        Expense.objects.filter(
            business_id=bid, status=ExpenseStatus.POSTED,
            expense_date__gte=s_date, expense_date__lte=e_date),
        "expense_date", "amount", period)
    with_salary = period == "month" and include_salary
    salary = (
        salary_cost_by_month(bid, s_date, e_date.replace(day=1))
        if period == "month" and include_salary else {}
    )

    book = _price_book(bid)
    tracked = set(
        Product._base_manager.filter(business_id=bid, track_stock=True)
        .values_list("pk", flat=True)
    )
    edges = [local_day_bounds(business, d)[0] for d in starts] + [end]
    values = [stock_value_at(bid, at, book, tracked) for at in edges]

    rows, totals = [], dict.fromkeys(
        ("sales", "purchases", "expenses", "salary", "inventory_change",
         "profit", "profit_stock_adjusted"), ZERO)
    unvalued_ids = set()
    for i, b in enumerate(starts):
        bucket_end = (b + timedelta(days=6)) if period == "week" else _month_next(b) - timedelta(days=1)
        sales = deliveries.get(b, ZERO) + shop.get(b, ZERO)
        purchases = milk.get(b, ZERO) + other.get(b, ZERO) + adjust.get(b, ZERO)
        spent = expenses.get(b, ZERO)
        sal = salary.get(b, ZERO)
        change = values[i + 1][0] - values[i][0]
        unvalued_ids |= set(values[i][1]) | set(values[i + 1][1])
        p = sales - purchases - spent - sal
        pa = sales - (purchases - change) - spent - sal
        rows.append({
            "from": b.isoformat(), "to": bucket_end.isoformat(),
            "sales": money(sales), "purchases": money(purchases),
            "expenses": money(spent),
            "salary": money(sal) if with_salary else None,
            "salary_included": with_salary,
            "inventory_change": money(change),
            "profit": money(p), "profit_stock_adjusted": money(pa),
        })
        for k, v in (("sales", sales), ("purchases", purchases), ("expenses", spent),
                     ("salary", sal), ("inventory_change", change),
                     ("profit", p), ("profit_stock_adjusted", pa)):
            totals[k] += v

    warnings = [
        "Operational profit ESTIMATE, not accounting profit: purchases are "
        "goods bought, not cost of goods sold.",
        "Sales are counted on delivery / sale date; collections and unpaid "
        "balances are not part of profit.",
    ]
    if period == "week":
        warnings.append("Salary is monthly and is not included in weekly figures.")
    elif not include_salary:
        warnings.append("Salary is owner-only data and is not included for your role.")
    if unvalued_ids:
        warnings.append(
            f"{len(unvalued_ids)} product(s) hold stock but have no purchase "
            "price; they are left out of inventory_change.")
    return {
        "business": {"id": str(bid), "name": business.name, "timezone": business.timezone},
        "period": period,
        "from": s_date.isoformat(), "to": e_date.isoformat(),
        "requested": {"from": from_date.isoformat(), "to": to_date.isoformat()},
        "basis": "live",
        "generated_at": timezone.now().isoformat(),
        "rows": rows,
        "totals": {k: money(v) for k, v in totals.items()},
        "unvalued_products": len(unvalued_ids),
        "warnings": warnings,
    }
