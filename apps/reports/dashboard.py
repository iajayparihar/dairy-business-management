"""
Daily summary and profit estimate. Read-only, live (recomputed from ledgers).

Every figure comes from one grouped query over the range [start, end) in
Business.timezone. Definitions are in ROADMAP.md (Phase 9) and
ARCHITECTURE.md; in short:
  * purchases = milk collections (RECORDED) + RECEIVED non-milk purchases
    + purchase adjustments. A milk collection owns a mirror RECEIVED
    SupplierPurchase of the same amount; that mirror is never summed.
  * sales = DELIVERED deliveries + COMPLETED shop sales (all modes), by
    delivered_at / sold_at. Independent of billing and of payment.
  * collections = shop CASH / ONLINE sales + customer payments net of
    reversals (by paid_at / reversed_at).
"""
from django.db.models import Exists, OuterRef, Sum
from django.utils import timezone

from apps.billing.models import (
    CustomerBill,
    CustomerBillLine,
    CustomerPayment,
    CustomerPaymentReversal,
)
from apps.deliveries.models import Delivery
from apps.expenses.models import Expense, Status as ExpenseStatus
from apps.expenses.selectors import salary_cost_by_month
from apps.inventory.models import BusinessMilkConfig
from apps.inventory.services import stock_balances
from apps.payments.models import SupplierPayment, SupplierPaymentReversal
from apps.products.models import Product
from apps.purchases.milk_queries import local_day_bounds
from apps.purchases.models import (
    MilkCollection,
    PurchaseAdjustment,
    SupplierPurchase,
)
from apps.reports.common import BUCKETS, ZERO, ZERO_QTY, bucket_of, money, qty
from apps.reports.positions import customer_positions, supplier_positions
from apps.sales.models import Sale, SaleLine


def _sum(queryset, field):
    return queryset.aggregate(t=Sum(field))["t"] or ZERO


def _buckets(rows):
    """[(method, amount)] -> {"cash": x, "online": y, "other": z}."""
    out = {b: ZERO for b in BUCKETS}
    for method, amount in rows:
        out[bucket_of(method)] += amount or ZERO
    return out


def milk_product_ids(business_id):
    config = BusinessMilkConfig.objects.filter(business_id=business_id).first()
    if config is None:
        return set()
    return {p for p in (config.cow_product_id, config.buffalo_product_id) if p}


def _net_by_method(payments, reversals, method_field, rev_method_field):
    """Payments minus reversals, bucketed by payment method."""
    paid = payments.order_by().values(method_field).annotate(t=Sum("amount"))
    back = reversals.order_by().values(rev_method_field).annotate(t=Sum("amount"))
    gross = _buckets((r[method_field], r["t"]) for r in paid)
    reversed_ = _buckets((r[rev_method_field], r["t"]) for r in back)
    return {b: gross[b] - reversed_[b] for b in BUCKETS}


def period_totals(business, start, end):
    """All flow figures for [start, end) (aware datetimes)."""
    bid = business.pk
    milk_ids = milk_product_ids(bid)

    # ---- purchases
    collections = MilkCollection.objects.filter(
        business_id=bid, status=MilkCollection.Status.RECORDED,
        collected_at__gte=start, collected_at__lt=end,
    )
    milk = collections.aggregate(litres=Sum("litres"), amount=Sum("amount"))
    by_type = {
        r["milk_type"]: r["litres"]
        for r in collections.order_by().values("milk_type").annotate(litres=Sum("litres"))
    }
    received = SupplierPurchase.objects.filter(
        business_id=bid, status=SupplierPurchase.Status.RECEIVED, is_deleted=False,
    )
    other_purchases = _sum(
        received.filter(
            milk_collection__isnull=True,
            purchased_at__gte=start, purchased_at__lt=end,
        ),
        "total_amount",
    )
    adjustments = _sum(
        PurchaseAdjustment.objects.filter(
            business_id=bid, purchase__in=received,
            occurred_at__gte=start, occurred_at__lt=end,
        ),
        "amount",
    )
    milk_cost = milk["amount"] or ZERO
    other_cost = other_purchases + adjustments

    # ---- sales
    deliveries = Delivery.objects.filter(
        business_id=bid, status=Delivery.Status.DELIVERED,
        delivered_at__gte=start, delivered_at__lt=end,
    )
    delivery_amount = _sum(deliveries, "line_total")
    shop = Sale.objects.filter(
        business_id=bid, status=Sale.Status.COMPLETED,
        sold_at__gte=start, sold_at__lt=end,
    )
    shop_by_mode = {m: ZERO for m in Sale.PaymentMode.values}
    for r in shop.order_by().values("payment_mode").annotate(t=Sum("total_amount")):
        shop_by_mode[r["payment_mode"]] = r["t"]
    litres_delivered = _sum(deliveries.filter(product_id__in=milk_ids), "quantity")
    litres_shop = _sum(
        SaleLine.objects.filter(
            sale__in=shop, product_id__in=milk_ids,
        ),
        "quantity",
    )
    sales_total = delivery_amount + sum(shop_by_mode.values(), ZERO)

    # ---- collections and supplier payments
    customer_pay = _net_by_method(
        CustomerPayment.objects.filter(
            business_id=bid, paid_at__gte=start, paid_at__lt=end
        ),
        CustomerPaymentReversal.objects.filter(
            business_id=bid, reversed_at__gte=start, reversed_at__lt=end
        ),
        "payment_method", "payment__payment_method",
    )
    supplier_pay = _net_by_method(
        SupplierPayment.objects.filter(
            business_id=bid, paid_at__gte=start, paid_at__lt=end
        ),
        SupplierPaymentReversal.objects.filter(
            business_id=bid, reversed_at__gte=start, reversed_at__lt=end
        ),
        "payment_method", "payment__payment_method",
    )
    shop_cash = shop_by_mode[Sale.PaymentMode.CASH]
    shop_online = shop_by_mode[Sale.PaymentMode.ONLINE]
    collected = {
        "cash": customer_pay["cash"] + shop_cash,
        "online": customer_pay["online"] + shop_online,
        "other": customer_pay["other"],
    }

    return {
        "milk_litres": milk["litres"] or ZERO_QTY,
        "milk_litres_by_type": by_type,
        "milk_cost": milk_cost,
        "other_purchase_cost": other_cost,
        "purchase_cost": milk_cost + other_cost,
        "litres_sold": litres_delivered + litres_shop,
        "sales_deliveries": delivery_amount,
        "sales_shop": shop_by_mode,
        "sales_total": sales_total,
        "collections": collected,
        "collections_total": sum(collected.values(), ZERO),
        "collections_detail": {
            "customer_payments": customer_pay,
            "shop_cash": shop_cash,
            "shop_online": shop_online,
        },
        "supplier_payments": supplier_pay,
        "supplier_payments_total": sum(supplier_pay.values(), ZERO),
    }


def expenses_between(business_id, start_date, end_date):
    """POSTED expenses between two local dates, inclusive."""
    return _sum(
        Expense.objects.filter(
            business_id=business_id, status=ExpenseStatus.POSTED,
            expense_date__gte=start_date, expense_date__lte=end_date,
        ),
        "amount",
    )


def salary_between(business_id, start_date, end_date):
    """Salary cost of the calendar months that touch [start_date, end_date]."""
    first = start_date.replace(day=1)
    last = end_date.replace(day=1)
    return sum(salary_cost_by_month(business_id, first, last).values(), ZERO)


def _unbilled(business_id, end):
    """Delivered / credit-sold before `end`, not on any FINALIZED bill (now)."""
    on_bill = CustomerBill.Status.FINALIZED
    deliveries = Delivery.objects.filter(
        business_id=business_id, status=Delivery.Status.DELIVERED,
        delivered_at__lt=end,
    ).exclude(
        Exists(CustomerBillLine.objects.filter(
            delivery=OuterRef("pk"), bill__status=on_bill))
    )
    sales = Sale.objects.filter(
        business_id=business_id, status=Sale.Status.COMPLETED,
        payment_mode=Sale.PaymentMode.CREDIT, sold_at__lt=end,
    ).exclude(
        Exists(CustomerBillLine.objects.filter(
            sale_line__sale=OuterRef("pk"), bill__status=on_bill))
    )
    return _sum(deliveries, "line_total") + _sum(sales, "total_amount")


def _fmt_buckets(d):
    return {k: money(v) for k, v in d.items()}


def daily_summary(business, day):
    start, end = local_day_bounds(business, day)
    t = period_totals(business, start, end)
    bid = business.pk
    expenses = expenses_between(bid, day, day)

    customers = customer_positions(bid, end).values()
    suppliers = supplier_positions(bid, end).values()
    balances = stock_balances(business_id=bid, before=end)
    tracked = set(
        Product._base_manager.filter(business_id=bid, track_stock=True)
        .values_list("pk", flat=True)
    )
    negative = sum(1 for pid, v in balances.items() if pid in tracked and v < 0)

    warnings = []
    if not milk_product_ids(bid):
        warnings.append("Cow/Buffalo milk products are not mapped: litres_sold is 0.")
    warnings.append(
        "Salary is monthly and is shown only in the monthly profit view."
    )

    return {
        "business": {"id": str(bid), "name": business.name,
                     "timezone": business.timezone, "currency": business.currency},
        "date": day.isoformat(),
        "basis": "live",
        "generated_at": timezone.now().isoformat(),
        "milk": {
            "litres_bought": qty(t["milk_litres"]),
            "litres_bought_by_type": {k: qty(v) for k, v in t["milk_litres_by_type"].items()},
            "litres_sold": qty(t["litres_sold"]),
        },
        "purchases": {
            "milk_cost": money(t["milk_cost"]),
            "other_cost": money(t["other_purchase_cost"]),
            "total": money(t["purchase_cost"]),
        },
        "sales": {
            "deliveries": money(t["sales_deliveries"]),
            "shop_cash": money(t["sales_shop"]["CASH"]),
            "shop_online": money(t["sales_shop"]["ONLINE"]),
            "shop_credit": money(t["sales_shop"]["CREDIT"]),
            "total": money(t["sales_total"]),
        },
        "collections": {
            **_fmt_buckets(t["collections"]),
            "total": money(t["collections_total"]),
            "from_shop_sales": {
                "cash": money(t["collections_detail"]["shop_cash"]),
                "online": money(t["collections_detail"]["shop_online"]),
            },
            "from_customer_payments": _fmt_buckets(
                t["collections_detail"]["customer_payments"]
            ),
        },
        "supplier_payments": {
            **_fmt_buckets(t["supplier_payments"]),
            "total": money(t["supplier_payments_total"]),
        },
        "expenses": money(expenses),
        "balances_at_end_of_day": {
            "receivable_outstanding": money(sum((p["outstanding_amount"] for p in customers), ZERO)),
            "customer_advances": money(sum((p["available_credit"] for p in customers), ZERO)),
            "payable_outstanding": money(sum((p["outstanding_amount"] for p in suppliers), ZERO)),
            "supplier_advances": money(sum((p["available_credit"] for p in suppliers), ZERO)),
            "unbilled_sales": money(_unbilled(bid, end)),
        },
        "stock_health": {"negative_stock_products": negative},
        "warnings": warnings,
    }
