"""
The bill as a document: one dict feeds the API detail and the PDF.

A DRAFT is computed live from the deliveries. A FINALIZED or VOID bill reads
only what was stored at finalize (lines, balance_before, header_snapshot), so
it never changes afterwards.
"""
from decimal import Decimal

from django.utils import timezone

from apps.billing import selectors
from apps.billing.bill_services import build_bill_lines
from apps.billing.models import CustomerBill, CustomerBillLine
from apps.businesses.models import Business

ZERO = selectors.ZERO
Kind = CustomerBillLine.Kind


def money(value):
    return None if value is None else f"{Decimal(value):.2f}"


def quantity(value):
    return f"{Decimal(value):.3f}"


def payment_status(bill, paid):
    if bill.status != CustomerBill.Status.FINALIZED:
        return None
    if paid >= bill.total_amount:
        return "PAID"
    return "PARTIAL" if paid > 0 else "UNPAID"


def _stored_rows(bill):
    return [
        {
            "delivery_id": line.delivery_id,
            "sale_line_id": line.sale_line_id,
            "product_id": line.product_id,
            "line_kind": line.line_kind,
            "description": line.description,
            "unit": line.unit,
            "delivered_at": line.delivered_at,
            "shift": line.shift,
            "quantity": line.quantity,
            "unit_price": line.unit_price,
            "amount": line.amount,
        }
        for line in bill.lines.all()
    ]


def summarize(rows):
    """Group lines by kind, product and rate; return (summary, totals)."""
    groups = {}
    for row in rows:
        key = (
            row["line_kind"], row["description"], row["unit"], row["unit_price"],
        )
        group = groups.setdefault(key, {"quantity": ZERO, "amount": ZERO})
        group["quantity"] += row["quantity"]
        group["amount"] += row["amount"]

    order = {Kind.COW_MILK: 0, Kind.BUFFALO_MILK: 1, Kind.OTHER: 2}
    summary = [
        {
            "kind": kind, "product": description, "unit": unit,
            "rate": unit_price, "quantity": group["quantity"],
            "amount": group["amount"],
        }
        for (kind, description, unit, unit_price), group in sorted(
            groups.items(), key=lambda item: (order[item[0][0]], item[0][1], item[0][3])
        )
    ]
    totals = {
        "cow_litres": sum((r["quantity"] for r in rows if r["line_kind"] == Kind.COW_MILK), ZERO),
        "buffalo_litres": sum((r["quantity"] for r in rows if r["line_kind"] == Kind.BUFFALO_MILK), ZERO),
        "other_amount": sum((r["amount"] for r in rows if r["line_kind"] == Kind.OTHER), ZERO),
        "total": sum((r["amount"] for r in rows), ZERO),
    }
    return summary, totals


def bill_header(bill):
    return {
        "id": str(bill.pk),
        "bill_number": bill.bill_number,
        "bill_type": bill.bill_type,
        "status": bill.status,
        "customer_id": str(bill.customer_id),
        "period_start": bill.period_start,
        "period_end": bill.period_end,
        "total_amount": money(bill.total_amount),
        "finalized_at": bill.finalized_at,
        "voided_at": bill.voided_at,
        "void_reason": bill.void_reason,
        "replaces": str(bill.replaces_id) if bill.replaces_id else None,
        "created_at": bill.created_at,
    }


def bill_document(bill):
    """Full bill: header, lines, grouped summary, totals, account, warnings."""
    live = bill.status == CustomerBill.Status.DRAFT
    warnings = []

    if live:
        business = Business.objects.get(pk=bill.business_id)
        rows, warnings = build_bill_lines(
            business, bill.customer_id, bill.period_start, bill.period_end
        )
        snapshot = {}
        balance_before = selectors.get_customer_position(
            business_id=bill.business_id, customer_id=bill.customer_id
        )["net_balance"]
    else:
        rows = _stored_rows(bill)
        snapshot = bill.header_snapshot
        balance_before = bill.balance_before

    summary, totals = summarize(rows)
    paid = due = None

    if bill.status == CustomerBill.Status.FINALIZED:
        paid = selectors.allocated_to_bill(bill.business_id, bill)
        due = bill.total_amount - paid

    document = bill_header(bill)
    if live:
        document["total_amount"] = money(totals["total"])
    document.update({
        "live": live,
        "header": snapshot,
        "lines": [
            {
                "delivered_at": r["delivered_at"], "shift": r["shift"],
                "source": "SHOP" if r["sale_line_id"] else "DELIVERY",
                "kind": r["line_kind"], "product": r["description"],
                "unit": r["unit"], "quantity": quantity(r["quantity"]),
                "rate": money(r["unit_price"]), "amount": money(r["amount"]),
            }
            for r in rows
        ],
        "summary": [
            {**s, "rate": money(s["rate"]), "quantity": quantity(s["quantity"]),
             "amount": money(s["amount"])}
            for s in summary
        ],
        "totals": {
            "cow_litres": quantity(totals["cow_litres"]),
            "buffalo_litres": quantity(totals["buffalo_litres"]),
            "other_amount": money(totals["other_amount"]),
            "total": money(totals["total"]),
        },
        "account": {
            "balance_before": money(balance_before),
            "balance_after": money(balance_before + totals["total"]),
            "paid_amount": money(paid),
            "due_amount": money(due),
            "payment_status": payment_status(bill, paid) if paid is not None else None,
        },
        "warnings": warnings,
        "generated_at": timezone.now(),
    })
    return document
