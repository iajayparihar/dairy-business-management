"""
Tabular reports. Each builder returns one dict (a "table"):

    {"key", "title", "period", "columns": [(key, label, kind)], "rows": [dict],
     "totals": {column key: value} , "meta": {...}, "warnings": [...]}

kind is text | money | qty | date | datetime. Rows hold raw Python values; the
JSON view, the CSV export and the PDF export all format the SAME table, so
what is on screen is what is exported. Everything is read-only and live.
"""
from decimal import Decimal

from django.db.models import Case, Count, Exists, F, Max, OuterRef, Q, Sum, Value, When
from django.db.models.functions import Coalesce, TruncDate

from apps.billing.models import CustomerBill, CustomerBillLine, CustomerPayment
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.expenses.models import Expense, ExpenseCategory, Status as ExpenseStatus
from apps.inventory.models import StockMovement
from apps.inventory.services import STOCK_IN_TYPES, STOCK_OUT_TYPES, stock_balances
from apps.payments.statements import get_supplier_account_statement
from apps.products.models import Product
from apps.reports.common import ZERO, ZERO_QTY, bucket_of
from apps.reports.positions import customer_positions, supplier_positions
from apps.sales.models import Sale, SaleLine
from apps.suppliers.models import Supplier

MT = StockMovement.MovementType


def _table(key, title, columns, rows, totals=None, meta=None, warnings=None):
    return {
        "key": key, "title": title, "columns": columns, "rows": rows,
        "totals": totals or {}, "meta": meta or {}, "warnings": warnings or [],
    }


def _sum(rows, key):
    return sum((r[key] or ZERO for r in rows), ZERO)


# ------------------------------------------------------------ supplier side

def supplier_balances(business, as_of=None, only_due=False):
    positions = supplier_positions(business.pk, as_of)
    suppliers = Supplier.all_objects.filter(business=business).order_by("name", "supplier_code")
    rows = []
    for s in suppliers:
        p = positions.get(s.pk)
        if p is None:
            continue
        if only_due and p["outstanding_amount"] <= 0:
            continue
        rows.append({
            "code": s.supplier_code, "name": s.name,
            "purchases": p["total_purchases"], "payments": p["total_payments"],
            "outstanding": p["outstanding_amount"], "advance": p["available_credit"],
        })
    cols = [("code", "Code", "text"), ("name", "Supplier", "text"),
            ("purchases", "Purchases", "money"), ("payments", "Paid", "money"),
            ("outstanding", "Outstanding", "money"), ("advance", "Advance", "money")]
    totals = {k: _sum(rows, k) for k in ("purchases", "payments", "outstanding", "advance")}
    return _table("supplier-balances", "Supplier balances", cols, rows, totals,
                  {"as_of": as_of})


def supplier_ledger(business, supplier, start_date, end_date):
    st = get_supplier_account_statement(
        business_id=business.pk, supplier_id=supplier.pk,
        start_date=start_date, end_date=end_date,
    )
    rows = [{
        "at": t["timestamp"], "type": t["type"], "reference": t["reference"],
        "description": t["description"], "debit": t["debit"],
        "credit": t["credit"], "balance": t["balance"],
    } for t in st["transactions"]]
    cols = [("at", "Date", "datetime"), ("type", "Type", "text"),
            ("reference", "Reference", "text"), ("description", "Description", "text"),
            ("debit", "We owe more", "money"), ("credit", "We owe less", "money"),
            ("balance", "Balance", "money")]
    return _table(
        "supplier-ledger", f"Supplier ledger - {supplier.name}", cols, rows,
        {"debit": _sum(rows, "debit"), "credit": _sum(rows, "credit")},
        {"supplier": {"id": str(supplier.pk), "code": supplier.supplier_code, "name": supplier.name},
         "opening_balance": st["opening_balance"], "closing_balance": st["closing_balance"],
         "outstanding_amount": st["outstanding_amount"], "available_credit": st["available_credit"]},
        ["Positive balance = we owe the supplier."],
    )


# ------------------------------------------------------------ customer side

def _unbilled(business_id, as_of):
    on_bill = CustomerBill.Status.FINALIZED
    deliveries = Delivery.objects.filter(
        business_id=business_id, status=Delivery.Status.DELIVERED
    ).exclude(Exists(CustomerBillLine.objects.filter(
        delivery=OuterRef("pk"), bill__status=on_bill)))
    sales = Sale.objects.filter(
        business_id=business_id, status=Sale.Status.COMPLETED,
        payment_mode=Sale.PaymentMode.CREDIT, customer__isnull=False,
    ).exclude(Exists(CustomerBillLine.objects.filter(
        sale_line__sale=OuterRef("pk"), bill__status=on_bill)))
    if as_of:
        deliveries = deliveries.filter(delivered_at__lt=as_of)
        sales = sales.filter(sold_at__lt=as_of)
    out = {}
    for qs, key, field in ((deliveries, "customer_id", "line_total"),
                           (sales, "customer_id", "total_amount")):
        for r in qs.order_by().values(key).annotate(t=Sum(field)):
            out[r[key]] = out.get(r[key], ZERO) + (r["t"] or ZERO)
    return out


def customer_outstanding(business, as_of=None, customer_type=None,
                         only_due=False, ordering="-outstanding"):
    positions = customer_positions(business.pk, as_of)
    unbilled = _unbilled(business.pk, as_of)
    payments = CustomerPayment.objects.filter(business_id=business.pk)
    if as_of:
        payments = payments.filter(paid_at__lt=as_of)
    last_paid = {r["customer_id"]: r["m"] for r in
                 payments.order_by().values("customer_id").annotate(m=Max("paid_at"))}
    customers = Customer.all_objects.filter(business=business)
    if customer_type:
        customers = customers.filter(customer_type=customer_type)
    rows = []
    for c in customers:
        p = positions.get(c.pk)
        ub = unbilled.get(c.pk, ZERO)
        if p is None and ub == 0:
            continue
        p = p or {"total_billed": ZERO, "total_payments": ZERO,
                  "outstanding_amount": ZERO, "available_credit": ZERO, "net_balance": ZERO}
        if only_due and p["outstanding_amount"] <= 0:
            continue
        rows.append({
            "code": c.customer_code, "name": c.name, "type": c.customer_type,
            "billed": p["total_billed"], "payments": p["total_payments"],
            "outstanding": p["outstanding_amount"], "advance": p["available_credit"],
            "net_balance": p["net_balance"], "unbilled": ub,
            "last_payment": last_paid.get(c.pk),
        })
    keys = {"outstanding": "outstanding", "name": "name", "code": "code"}
    field = keys.get(ordering.lstrip("-"), "outstanding")
    rows.sort(key=lambda r: (r[field] if field != "name" else r[field].lower(), r["code"]),
              reverse=ordering.startswith("-"))
    cols = [("code", "Code", "text"), ("name", "Customer", "text"), ("type", "Type", "text"),
            ("billed", "Billed", "money"), ("payments", "Paid", "money"),
            ("outstanding", "Outstanding", "money"), ("advance", "Advance", "money"),
            ("net_balance", "Net balance", "money"), ("unbilled", "Unbilled (not in balance)", "money"),
            ("last_payment", "Last payment", "datetime")]
    totals = {k: _sum(rows, k) for k in
              ("billed", "payments", "outstanding", "advance", "net_balance", "unbilled")}
    return _table("customer-outstanding", "Customer outstanding", cols, rows, totals,
                  {"as_of": as_of},
                  ["Net balance: positive = customer owes, negative = advance.",
                   "Unbilled deliveries / credit sales are shown beside the balance, never added to it."])


# -------------------------------------------------------------------- stock

_STOCK_COLS = [
    (MT.PURCHASE_RECEIPT, "bought"), (MT.PRODUCTION_IN, "produced"),
    (MT.ADJUSTMENT_IN, "adjust_in"), (MT.DELIVERY, "delivered"),
    (MT.SALE, "sold_shop"), (MT.PRODUCTION_OUT, "consumed"),
    (MT.WASTAGE, "wastage"), (MT.ADJUSTMENT_OUT, "adjust_out"),
]


def stock_report(business, start, end, product_id=None, only_flagged=False):
    """start / end: aware half-open bounds. Quantities only, never summed across units."""
    moves = StockMovement.objects.filter(
        business_id=business.pk, occurred_at__gte=start, occurred_at__lt=end)
    if product_id:
        moves = moves.filter(product_id=product_id)
    eff = Coalesce(F("reversal_of__movement_type"), F("movement_type"))
    signed = Case(
        When(movement_type__in=STOCK_IN_TYPES, then=F("quantity")),
        default=-F("quantity"), output_field=StockMovement._meta.get_field("quantity"),
    )
    flow = {}
    for r in moves.order_by().annotate(eff=eff, s=signed).values("product_id", "eff").annotate(t=Sum("s")):
        flow.setdefault(r["product_id"], {})[r["eff"]] = r["t"] or ZERO_QTY
    opening = stock_balances(business_id=business.pk, before=start)
    closing = stock_balances(business_id=business.pk, before=end)
    ids = set(flow) | {p for p, v in opening.items() if v} | {p for p, v in closing.items() if v}
    if product_id:
        ids &= {product_id}
    products = Product._base_manager.filter(business=business, track_stock=True, pk__in=ids)\
        .order_by("name", "sku")
    rows = []
    for p in products:
        f = flow.get(p.pk, {})
        row = {"sku": p.sku, "name": p.name, "unit": p.unit,
               "opening": opening.get(p.pk, ZERO_QTY)}
        for mt, key in _STOCK_COLS:
            v = f.get(mt, ZERO_QTY)
            row[key] = v if mt in STOCK_IN_TYPES else -v
        known = {mt for mt, _ in _STOCK_COLS}
        row["other"] = sum((v for k, v in f.items() if k not in known), ZERO_QTY)
        row["closing"] = closing.get(p.pk, ZERO_QTY)
        row["is_negative"] = "YES" if row["closing"] < 0 else ""
        row["deleted"] = "YES" if p.is_deleted else ""
        if only_flagged and not row["is_negative"]:
            continue
        rows.append(row)
    cols = [("sku", "SKU", "text"), ("name", "Product", "text"), ("unit", "Unit", "text"),
            ("opening", "Opening", "qty")]
    labels = {"bought": "Bought", "produced": "Produced", "adjust_in": "Adjust in",
              "delivered": "Delivered", "sold_shop": "Shop sales", "consumed": "Used in production",
              "wastage": "Wastage", "adjust_out": "Adjust out"}
    cols += [(k, labels[k], "qty") for _, k in _STOCK_COLS]
    cols += [("other", "Other", "qty"), ("closing", "Closing", "qty"),
             ("is_negative", "Negative?", "text"), ("deleted", "Product deleted?", "text")]
    return _table("stock", "Stock report", cols, rows, {},
                  {}, ["Quantities are per product unit; nothing is added across units.",
                       "Out columns are shown as positive quantities. Reversals sit under the original type."])


# ------------------------------------------------------------ product sales

def product_sales(business, start, end, product_id=None, group_by="product"):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(business.timezone)
    dq = Delivery.objects.filter(
        business_id=business.pk, status=Delivery.Status.DELIVERED,
        delivered_at__gte=start, delivered_at__lt=end)
    sq = SaleLine.objects.filter(
        sale__business_id=business.pk, sale__status=Sale.Status.COMPLETED,
        sale__sold_at__gte=start, sale__sold_at__lt=end)
    if product_id:
        dq, sq = dq.filter(product_id=product_id), sq.filter(product_id=product_id)

    acc = {}

    def add(day, pid, name, sku, unit, source, quantity, amount):
        key = (day if group_by == "day" else None, pid or f"other:{name}")
        r = acc.setdefault(key, {
            "date": key[0], "sku": sku, "name": name, "unit": unit,
            "qty_delivery": ZERO_QTY, "qty_shop": ZERO_QTY,
            "amount_delivery": ZERO, "amount_shop": ZERO})
        r[f"qty_{source}"] += quantity
        r[f"amount_{source}"] += amount

    d_day = TruncDate("delivered_at", tzinfo=tz)
    s_day = TruncDate("sale__sold_at", tzinfo=tz)
    names = {p.pk: p for p in Product._base_manager.filter(business=business)}
    for r in dq.order_by().annotate(d=d_day).values("d", "product_id")\
            .annotate(q=Sum("quantity"), a=Sum("line_total")):
        p = names[r["product_id"]]
        add(r["d"], p.pk, p.name, p.sku, p.unit, "delivery", r["q"], r["a"])
    for r in sq.order_by().annotate(d=s_day).values("d", "product_id", "description", "unit")\
            .annotate(q=Sum("quantity"), a=Sum("line_total")):
        p = names.get(r["product_id"])
        if p:
            add(r["d"], p.pk, p.name, p.sku, p.unit, "shop", r["q"], r["a"])
        else:
            add(r["d"], None, r["description"] or "Other item", "", r["unit"] or "", "shop", r["q"], r["a"])

    rows = []
    for r in acc.values():
        r["qty_total"] = r["qty_delivery"] + r["qty_shop"]
        r["amount_total"] = r["amount_delivery"] + r["amount_shop"]
        r["avg_price"] = (r["amount_total"] / r["qty_total"]).quantize(Decimal("0.01")) \
            if r["qty_total"] else ZERO
        rows.append(r)
    rows.sort(key=lambda r: (r["date"].toordinal() if r["date"] else 0, r["name"].lower()))
    cols = ([("date", "Date", "date")] if group_by == "day" else []) + [
        ("sku", "SKU", "text"), ("name", "Product", "text"), ("unit", "Unit", "text"),
        ("qty_delivery", "Qty delivered", "qty"), ("qty_shop", "Qty shop", "qty"),
        ("qty_total", "Qty total", "qty"),
        ("amount_delivery", "Amount delivered", "money"), ("amount_shop", "Amount shop", "money"),
        ("amount_total", "Net sales", "money"), ("avg_price", "Avg price", "money")]
    return _table("product-sales", "Product-wise sales", cols, rows,
                  {k: _sum(rows, k) for k in ("amount_delivery", "amount_shop", "amount_total")},
                  {"discounts_returns_supported": False},
                  ["Discounts and sales returns do not exist in this system: net sales = gross sales.",
                   "Cancelled deliveries and sales are excluded; quantities of different units are never added."])


# ----------------------------------------------------------------- expenses

def expense_report(business, start_date, end_date, category_id=None,
                   payment_method=None, group_by=None):
    qs = Expense.objects.filter(
        business=business, status=ExpenseStatus.POSTED,
        expense_date__gte=start_date, expense_date__lte=end_date)
    if category_id:
        qs = qs.filter(category_id=category_id)
    if payment_method:
        qs = qs.filter(payment_method=payment_method)
    warnings = ["Salary is separate owner-only data and is not part of this report."]
    if group_by == "category":
        data = qs.order_by().values("category__name").annotate(n=Count("id"), t=Sum("amount")).order_by("category__name")
        rows = [{"category": r["category__name"], "count": r["n"], "amount": r["t"]} for r in data]
        cols = [("category", "Category", "text"), ("count", "Entries", "text"), ("amount", "Amount", "money")]
    elif group_by == "day":
        data = qs.order_by().values("expense_date").annotate(n=Count("id"), t=Sum("amount")).order_by("expense_date")
        rows = [{"date": r["expense_date"], "count": r["n"], "amount": r["t"]} for r in data]
        cols = [("date", "Date", "date"), ("count", "Entries", "text"), ("amount", "Amount", "money")]
    else:
        rows = [{
            "date": e.expense_date, "number": e.expense_number, "category": e.category.name,
            "payee": e.payee, "description": e.description, "method": e.payment_method,
            "amount": e.amount,
        } for e in qs.select_related("category").order_by("expense_date", "created_at")]
        cols = [("date", "Date", "date"), ("number", "Number", "text"), ("category", "Category", "text"),
                ("payee", "Payee", "text"), ("description", "Description", "text"),
                ("method", "Method", "text"), ("amount", "Amount", "money")]
    by_bucket = {"cash": ZERO, "online": ZERO, "other": ZERO}
    for r in qs.order_by().values("payment_method").annotate(t=Sum("amount")):
        by_bucket[bucket_of(r["payment_method"])] += r["t"]
    return _table("expenses", "Expense report", cols, rows, {"amount": _sum(rows, "amount")},
                  {"by_payment_bucket": by_bucket}, warnings)


# ------------------------------------------- dashboard figures as tables

def _d(value):
    return None if value is None else Decimal(value)


def daily_table(s):
    """The daily summary (apps/reports/dashboard.py) as a two-value table."""
    rows = []

    def amount(label, value):
        rows.append({"metric": label, "amount": _d(value), "litres": None})

    def litres(label, value):
        rows.append({"metric": label, "amount": None, "litres": _d(value)})

    litres("Litres bought", s["milk"]["litres_bought"])
    litres("Litres sold (cow/buffalo milk)", s["milk"]["litres_sold"])
    amount("Purchase - milk", s["purchases"]["milk_cost"])
    amount("Purchase - other", s["purchases"]["other_cost"])
    amount("Purchase - total", s["purchases"]["total"])
    amount("Sales - deliveries", s["sales"]["deliveries"])
    amount("Sales - shop cash", s["sales"]["shop_cash"])
    amount("Sales - shop online", s["sales"]["shop_online"])
    amount("Sales - shop credit", s["sales"]["shop_credit"])
    amount("Sales - total", s["sales"]["total"])
    for k in ("cash", "online", "other", "total"):
        amount(f"Collected - {k}", s["collections"][k])
    for k in ("cash", "online", "other", "total"):
        amount(f"Paid to suppliers - {k}", s["supplier_payments"][k])
    amount("Expenses (entries)", s["expenses"])
    b = s["balances_at_end_of_day"]
    for k, label in (("receivable_outstanding", "Customers owe (end of day)"),
                     ("customer_advances", "Customer advances"),
                     ("payable_outstanding", "We owe suppliers"),
                     ("supplier_advances", "Supplier advances"),
                     ("unbilled_sales", "Unbilled sales (not in balances)")):
        amount(label, b[k])
    cols = [("metric", "Metric", "text"), ("amount", "Amount", "money"), ("litres", "Litres", "qty")]
    return _table("daily-summary", f"Daily summary {s['date']}", cols, rows, {},
                  {}, s["warnings"])


def profit_table(p):
    rows = [{
        "from": r["from"], "to": r["to"], "sales": _d(r["sales"]),
        "purchases": _d(r["purchases"]), "expenses": _d(r["expenses"]),
        "salary": _d(r["salary"]), "inventory_change": _d(r["inventory_change"]),
        "profit": _d(r["profit"]), "profit_stock_adjusted": _d(r["profit_stock_adjusted"]),
    } for r in p["rows"]]
    cols = [("from", "From", "date"), ("to", "To", "date"), ("sales", "Sales", "money"),
            ("purchases", "Purchases", "money"), ("expenses", "Expenses", "money"),
            ("salary", "Salary", "money"), ("inventory_change", "Stock value change", "money"),
            ("profit", "Profit (estimate)", "money"),
            ("profit_stock_adjusted", "Profit, stock-adjusted (estimate)", "money")]
    totals = {k: _d(v) for k, v in p["totals"].items()}
    return _table("profit", f"Profit estimate by {p['period']}", cols, rows, totals,
                  {}, p["warnings"])
