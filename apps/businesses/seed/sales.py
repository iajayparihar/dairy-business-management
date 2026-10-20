"""
Shop counter sales: a handful of walk-in CASH / ONLINE sales every day, an
occasional open-price "Other item", and CREDIT sales to the kirana store
every other day (these land on its monthly bill). One sale is cancelled and
one corrected this week.
"""
from datetime import timedelta

from apps.sales.models import Sale
from apps.sales.services import cancel_sale, correct_sale, create_sale

from .base import at, history_days, is_past, rng, today, worker

# One basket per walk-in sale.
BASKETS = [
    [("MILK-PKT", "2")],
    [("MILK-PKT", "1"), ("DAHI", "0.5")],
    [("PANEER", "0.25")],
    [("GHEE", "1")],
    [("BUTTER", "0.5"), ("MILK-PKT", "2")],
    [("CHAAS", "2")],
    [("COW-MILK", "1")],
    [("BUF-MILK", "2")],
    [("DAHI", "1"), ("CHAAS", "1")],
]
CREDIT_BASKET = [("MILK-PKT", "20"), ("BUTTER", "1"), ("DAHI", "2")]


def _lines(t, basket):
    return [{"product_id": t.products[sku].pk, "quantity": qty} for sku, qty in basket]


def seed(t):
    biz = t.business
    for i, day in enumerate(history_days()):
        user = worker(t, day)
        count = max(1, round(rng.randint(4, 8) * t.scale))
        for n in range(count):
            sold_at = at(day, rng.randint(8, 20), rng.randint(0, 59))
            if not is_past(sold_at):
                continue
            lines = _lines(t, rng.choice(BASKETS))
            if rng.random() < 0.1:
                lines.append({"product_id": t.products["OTHER"].pk, "quantity": "1",
                              "unit_price": "10.00"})  # open price: ice pack
            create_sale(
                business_id=biz.pk, user=user, lines=lines, sold_at=sold_at,
                payment_mode="CASH" if rng.random() < 0.6 else "ONLINE",
                client_ref=f"seed-sale-{day:%Y%m%d}-{n}",
            )
        if i % 2 == 0 and is_past(at(day, 9)):
            create_sale(
                business_id=biz.pk, user=user, payment_mode="CREDIT",
                customer_id=t.credit_customer.pk, lines=_lines(t, CREDIT_BASKET),
                sold_at=at(day, 9), client_ref=f"seed-credit-{day:%Y%m%d}",
                notes="Weekly khata",
            )

    week = Sale.objects.filter(
        business=biz, status="COMPLETED", payment_mode="CASH",
        sold_at__gte=at(today() - timedelta(days=4), 0), sold_at__lt=at(today(), 0),
    ).order_by("sold_at", "pk")
    duplicate, wrong = week[:2]
    cancel_sale(
        business_id=biz.pk, sale_id=duplicate.pk, user=t.manager,
        reason="Rung up twice at the counter.",
    )
    correct_sale(
        business_id=biz.pk, sale_id=wrong.pk, user=t.manager,
        reason="Customer paid by UPI, not cash.", payment_mode="ONLINE",
        lines=[
            {"product_id": line.product_id, "quantity": line.quantity,
             "unit_price": line.unit_price if line.product.allow_open_price else None}
            for line in wrong.lines.select_related("product")
        ],
    )
