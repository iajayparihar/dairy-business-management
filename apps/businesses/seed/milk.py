"""
Milk collections: every supplier, every day, morning (05:30) and evening
(16:30) shift, one row per can (big suppliers bring two cans). Each
collection posts milk into stock through the per-business milk mapping.

Then one cancelled entry and one corrected entry in the last few days, so
the audit trail (reason, who, when) has something to show.
"""
from datetime import timedelta

from apps.purchases.models import MilkCollection
from apps.purchases.services import (
    cancel_milk_collection,
    correct_milk_collection,
    create_milk_collection,
)

from .base import at, dec, history_days, is_past, rng, today, vary

SHIFTS = (("MORNING", 5, 30), ("EVENING", 16, 30))
CAN_LITRES = 20


def seed(t):
    biz = t.business
    for day in history_days():
        for shift, hour, minute in SHIFTS:
            moment = at(day, hour, minute)
            if not is_past(moment):
                continue
            for supplier, milk in t.farmers:
                for milk_type, litres in milk.items():
                    total = vary(litres)
                    cans = 2 if total > CAN_LITRES else 1
                    for can in range(cans):
                        create_milk_collection(
                            business_id=biz.pk, supplier_id=supplier.pk,
                            milk_type=milk_type, litres=dec(total / cans, "0.1"),
                            fat=dec(rng.uniform(6.2, 7.8), "0.1") if milk_type == "BUFFALO" else None,
                            collected_at=moment + timedelta(minutes=can * 3),
                            shift=shift, user=t.manager,
                        )

    recent = MilkCollection.objects.filter(
        business=biz, status="RECORDED", collected_at__lt=at(today() - timedelta(days=1), 0),
    ).order_by("-collected_at", "pk")
    wrong, typo = list(recent[:6])[::5]
    cancel_milk_collection(
        business_id=biz.pk, collection_id=wrong.pk, user=t.manager,
        reason="Entered under the wrong supplier; farmer did not come this shift.",
    )
    correct_milk_collection(
        business_id=biz.pk, collection_id=typo.pk, user=t.manager,
        reason="Litres typed wrong; checked the can register.",
        litres=typo.litres + 2,
        **({"fat": typo.fat} if typo.milk_type == "BUFFALO" else {}),
    )
