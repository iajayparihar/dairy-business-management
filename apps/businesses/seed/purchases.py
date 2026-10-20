"""
Packaged-goods purchases from the distributor: one invoice a week, received
into stock. The newest invoice is left as a DRAFT (goods not arrived), one
duplicate draft is cancelled, and the last received invoice has a goods
return (damaged butter) through the purchase adjustment ledger.
"""
from datetime import timedelta

from apps.purchases.adjustments import return_purchase_goods
from apps.purchases.services import cancel_draft_purchase, create_purchase, receive_purchase

from .base import at, dec, history_start, is_past, money, today

# sku, quantity per week at full scale, + fixed weekly qty for the kirana khata, cost
WEEKLY = [("GHEE", 6, 1, 540), ("BUTTER", 6, 4, 390), ("MILK-PKT", 50, 70, 25)]
# Distributor bills a little above the counter cost for small dairies.
MARKUP = {"abhigyan": 1.0, "vij": 1.02, "gopal": 1.04}


def _lines(t):
    lines = []
    for sku, qty, khata, cost in WEEKLY:
        product = t.products[sku]
        qty = round(qty * t.scale) + khata
        lines.append({
            "product_id": product.pk, "quantity": dec(qty),
            "unit_price": money(cost * MARKUP[t.key]),
        })
    return lines


def seed(t):
    biz = t.business
    day, n, received = history_start(), 0, []
    while is_past(at(day, 10)):
        n += 1
        purchase = create_purchase(
            business_id=biz.pk, supplier_id=t.distributor.pk,
            purchase_number=f"MDD/{day:%y}/{n:04d}", purchased_at=at(day, 10),
            lines=_lines(t), user=t.manager, notes="Weekly packaged goods",
        )
        if day + timedelta(days=7) > today():
            break  # newest invoice: goods not received yet
        receive_purchase(purchase_id=purchase.pk, user=t.manager, business_id=biz.pk)
        received.append(purchase)
        day += timedelta(days=7)

    duplicate = create_purchase(
        business_id=biz.pk, supplier_id=t.distributor.pk,
        purchase_number=f"MDD/{day:%y}/{n:04d}-DUP", purchased_at=at(day, 10),
        lines=_lines(t), user=t.manager,
    )
    cancel_draft_purchase(
        purchase_id=duplicate.pk, user=t.manager, business_id=biz.pk,
        reason="Same invoice entered twice.",
    )

    butter = received[-1].lines.get(product__sku="BUTTER")
    return_purchase_goods(
        business_id=biz.pk, purchase_id=received[-1].pk, user=t.manager,
        reason="Butter packs damaged in transit; returned to distributor.",
        lines=[{"purchase_line_id": butter.pk, "quantity": dec(1)}],
    )
    t.received_purchases = received
