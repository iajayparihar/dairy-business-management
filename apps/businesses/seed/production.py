"""
Production and stock adjustments.

Sized from each tenant's own demand (standing orders + counter sales):
daily cow milk -> dahi and buffalo milk -> paneer, every 3 days dahi ->
chaas packets. Weekly milk wastage, one stock gain, one stock loss, one
cancelled adjustment and one cancelled (duplicate) production batch.
"""
from datetime import timedelta

from apps.inventory.adjustments import cancel_adjustment, create_adjustment
from apps.inventory.production import cancel_batch, create_batch

from .base import at, dec, history_days, is_past, rng, today

# Rough yields: 5 L buffalo milk -> 1 kg paneer; 1.1 L cow milk -> 1 kg dahi;
# 1 kg dahi -> 8 chaas packets.
PANEER_MILK, DAHI_MILK, CHAAS_PER_DAHI = 5, 1.1, 8


def standing(t, sku):
    """Quantity of `sku` on standing orders per day, all customers and shifts."""
    return sum(
        float(qty)
        for shifts in t.orders.values() for items in shifts.values()
        for product, qty in items if product.sku == sku
    )


def recipes(t):
    """[(every N days, inputs, outputs)] with quantities per batch."""
    chaas = max(3, round(8 * t.scale))
    dahi = standing(t, "DAHI") + 3 * t.scale + chaas / CHAAS_PER_DAHI / 3
    paneer = standing(t, "PANEER") + 0.5 * t.scale
    return [
        (1, [("COW-MILK", dahi * DAHI_MILK)], [("DAHI", dahi)]),
        (1, [("BUF-MILK", paneer * PANEER_MILK)], [("PANEER", paneer)]),
        (3, [("DAHI", chaas / CHAAS_PER_DAHI)], [("CHAAS", chaas)]),
    ]


def milk_need(t):
    """Litres per day by milk type: deliveries + production + counter sales."""
    need = {"COW": standing(t, "COW-MILK"), "BUFFALO": standing(t, "BUF-MILK")}
    for every, inputs, _ in recipes(t):
        for sku, qty in inputs:
            if sku.endswith("-MILK"):
                need["COW" if sku == "COW-MILK" else "BUFFALO"] += qty / every
    return {k: v + 1.5 * t.scale for k, v in need.items()}


def _side(t, items):
    return [
        {
            "product_id": str(t.products[sku].pk),
            "quantity": dec(round(qty) if t.products[sku].unit == "PACKET" else round(qty, 1)),
        }
        for sku, qty in items
    ]


def seed(t):
    biz = t.business
    for i, day in enumerate(history_days()):
        if not is_past(at(day, 11)):
            continue
        for every, inputs, outputs in recipes(t):
            if i % every:
                continue
            create_batch(
                business_id=biz.pk, user=t.manager,
                inputs=_side(t, inputs), outputs=_side(t, outputs),
                client_ref=f"seed-prd-{day:%Y%m%d}-{outputs[0][0]}",
                produced_at=at(day, 11), notes="Daily production",
            )
        if day.weekday() == 6:  # Sunday: sour milk thrown away
            create_adjustment(
                business_id=biz.pk, user=t.manager, kind="WASTAGE",
                product_id=t.products[rng.choice(["COW-MILK", "BUF-MILK"])].pk,
                quantity=dec(round(rng.uniform(1.5, 4), 1)),
                reason="Milk turned sour in the evening heat.",
                client_ref=f"seed-waste-{day:%Y%m%d}", occurred_at=at(day, 21),
            )

    last_week = today() - timedelta(days=5)
    create_adjustment(
        business_id=biz.pk, user=t.manager, kind="GAIN",
        product_id=t.products["GHEE"].pk, quantity=dec(0.5),
        reason="Monthly count: one extra ghee jar found in the cold room.",
        client_ref="seed-gain-1", occurred_at=at(last_week, 20),
    )
    create_adjustment(
        business_id=biz.pk, user=t.manager, kind="LOSS",
        product_id=t.products["MILK-PKT"].pk, quantity=dec(6),
        reason="Leaking packets found during stock count.",
        client_ref="seed-loss-1", occurred_at=at(last_week, 20),
    )
    mistake = create_adjustment(
        business_id=biz.pk, user=t.manager, kind="WASTAGE",
        product_id=t.products["PANEER"].pk, quantity=dec(2),
        reason="Paneer spoiled.", client_ref="seed-waste-mistake",
        occurred_at=at(last_week, 21),
    )
    cancel_adjustment(
        business_id=biz.pk, adjustment_id=mistake.pk, user=t.owner,
        reason="Wrong entry: paneer was fine, it was moved to the shop fridge.",
    )
    _, inputs, outputs = recipes(t)[0]
    duplicate = create_batch(
        business_id=biz.pk, user=t.manager,
        inputs=_side(t, inputs), outputs=_side(t, outputs),
        client_ref="seed-prd-duplicate", produced_at=at(last_week, 11, 5),
    )
    cancel_batch(
        business_id=biz.pk, batch_id=duplicate.pk, user=t.owner,
        reason="Batch was entered twice by mistake.",
    )
