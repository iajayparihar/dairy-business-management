"""
Milk suppliers (farmers + a collection centre), their effective-dated milk
rates, and one distributor for packaged goods.

Volumes are scaled to the tenant's demand. Rates rise on the 1st of this month: COW is a flat rate per litre, BUFFALO
is litres x fat x rate. Same supplier codes in every tenant on purpose.
"""
from datetime import timedelta
from decimal import Decimal

from apps.suppliers.models import Supplier
from apps.suppliers.services import create_supplier_milk_rate

from .base import at, history_start, money, this_month
from .production import milk_need

# code, name, village, type, {milk type: litres per shift}
SURPLUS = 1.06
FARMERS = [
    ("RAM01", "Ramesh Gurjar", "Sanwer", "FARMER", {"BUFFALO": 16, "COW": 10}),
    ("SHY02", "Shyam Lal Patidar", "Betma", "FARMER", {"COW": 20}),
    ("BHE03", "Bheru Singh Chouhan", "Depalpur", "FARMER", {"BUFFALO": 18}),
    ("KAN04", "Kanhaiya Lal Jat", "Hatod", "FARMER", {"BUFFALO": 12, "COW": 12}),
    ("MAN05", "Mangilal Malviya", "Rau", "FARMER", {"COW": 16}),
    ("SAN06", "Sanwer Dugdh Sahkari Samiti", "Sanwer", "COLLECTION_CENTER",
     {"BUFFALO": 25, "COW": 20}),
]
# Starting rate per milk type; +RAISE from this month.
RATE = {"COW": Decimal("38.00"), "BUFFALO": Decimal("7.50")}
RAISE = {"COW": Decimal("1.00"), "BUFFALO": Decimal("0.25")}


def seed(t):
    biz = t.business
    since = at(history_start() - timedelta(days=1), 0)
    raise_at = at(this_month(), 0)
    t.farmers = []  # (supplier, {milk type: litres per shift})

    for i, (code, name, village, kind, milk) in enumerate(FARMERS[: t.spec["farmers"]]):
        milk = dict(milk)
        supplier = Supplier.objects.create(
            business=biz, supplier_code=code, name=name, supplier_type=kind,
            phone=f"94250{i:05d}{len(t.key)}"[:10],
            address=f"Village {village}, {t.spec['city']} district",
        )
        for milk_type in milk:
            # Small per-supplier difference so rates are not all identical.
            base = RATE[milk_type] + money(i * (0.5 if milk_type == "COW" else 0.05))
            create_supplier_milk_rate(
                business_id=biz.pk, supplier_id=supplier.pk, milk_type=milk_type,
                rate=base, effective_from=since, effective_to=raise_at, user=t.owner,
            )
            create_supplier_milk_rate(
                business_id=biz.pk, supplier_id=supplier.pk, milk_type=milk_type,
                rate=base + RAISE[milk_type], effective_from=raise_at, user=t.owner,
            )
        t.farmers.append((supplier, milk))

    # Scale every farmer so the tenant buys ~6% more milk than it needs.
    need = milk_need(t)
    for milk_type in need:
        supply = sum(m.get(milk_type, 0) for _, m in t.farmers) * 2  # two shifts
        for _, milk in t.farmers:
            if milk_type in milk:
                milk[milk_type] = milk[milk_type] * need[milk_type] * SURPLUS / supply

    t.distributor = Supplier.objects.create(
        business=biz, supplier_code="MDD", name="Malwa Dairy Distributors",
        supplier_type="DISTRIBUTOR", phone="0731400" + str(len(t.key)) * 3,
        address=f"Transport Nagar, {t.spec['city']}",
        notes="Ghee, butter and milk packets. Weekly invoice.",
    )
