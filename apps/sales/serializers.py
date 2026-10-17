"""Plain output shapes for the sales API. Money values are JSON strings."""
from apps.billing.models import CustomerBill
from apps.sales.models import Sale


def money(value):
    return f"{value:.2f}"


def _billed_as(sale):
    """Number of the FINALIZED bill carrying this credit sale, else None."""
    for line in sale.lines.all():
        for bill_line in line.bill_lines.all():
            if bill_line.bill.status == CustomerBill.Status.FINALIZED:
                return bill_line.bill.bill_number
    return None


def serialize_line(line):
    posted = any(m.movement_type == "SALE" for m in line.stock_movements.all())
    return {
        "position": line.position,
        "product_id": str(line.product_id) if line.product_id else None,
        "description": line.description,
        "unit": line.unit,
        "quantity": f"{line.quantity:.3f}",
        "unit_price": money(line.unit_price),
        "line_total": money(line.line_total),
        "stock_shortfall": f"{line.stock_shortfall:.3f}",
        # False when the product does not track stock (nothing was posted).
        "stock_posted": posted,
    }


def serialize_sale(sale):
    customer = sale.customer
    replaced_by = getattr(sale, "replaced_by", None)
    return {
        "id": str(sale.pk),
        "sale_number": sale.sale_number,
        "status": sale.status,
        "payment_mode": sale.payment_mode,
        "sold_at": sale.sold_at,
        "total_amount": money(sale.total_amount),
        "customer": (
            {"id": str(customer.pk), "code": customer.customer_code,
             "name": customer.name}
            if customer else None
        ),
        "client_ref": sale.client_ref,
        "notes": sale.notes,
        "replaces": str(sale.replaces_id) if sale.replaces_id else None,
        "replaced_by": str(replaced_by.pk) if replaced_by else None,
        "cancelled_at": sale.cancelled_at,
        "cancel_reason": sale.cancel_reason,
        "billed_as": (
            _billed_as(sale)
            if sale.payment_mode == Sale.PaymentMode.CREDIT else None
        ),
        "has_shortfall": any(l.stock_shortfall > 0 for l in sale.lines.all()),
        "lines": [serialize_line(l) for l in sale.lines.all()],
    }


SALE_RELATED = ("lines__stock_movements", "lines__bill_lines__bill")


def sale_queryset(business_id):
    return (
        Sale.objects.filter(business_id=business_id)
        .select_related("customer", "replaced_by")
        .prefetch_related(*SALE_RELATED)
    )
