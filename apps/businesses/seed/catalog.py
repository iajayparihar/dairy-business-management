"""
Categories, products, selling + purchase (cost) prices and the milk stock mapping.

Same SKUs in every tenant (tenant-isolation checks), prices differ per tenant.
Milk selling prices rise on the 1st of this month, so last month's bills and
this month's deliveries show different snapshot prices. Paneer has a price
change scheduled for next week (through the real service).
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.inventory.services import set_milk_config
from apps.products.models import Product, ProductCategory, ProductPrice
from apps.products.services import change_product_price

from .base import at, history_start, money, this_month

U = Product.Unit
# sku, name, unit, category, selling, cost (None = no price), track stock, open price
PRODUCTS = [
    ("COW-MILK", "Cow Milk", U.LITRE, "Milk", 54, 40, True, False),
    ("BUF-MILK", "Buffalo Milk", U.LITRE, "Milk", 66, 52, True, False),
    ("DAHI", "Dahi (Curd)", U.KILOGRAM, "Milk Products", 80, 50, True, False),
    ("PANEER", "Fresh Paneer", U.KILOGRAM, "Milk Products", 360, 260, True, False),
    ("CHAAS", "Chaas 500 ml", U.PACKET, "Milk Products", 15, 7, True, False),
    ("GHEE", "Desi Ghee", U.KILOGRAM, "Packaged", 650, 540, True, False),
    ("BUTTER", "White Butter", U.KILOGRAM, "Packaged", 480, 390, True, False),
    ("MILK-PKT", "Toned Milk Packet 500 ml", U.PACKET, "Packaged", 28, 25, True, False),
    ("OTHER", "Other item", U.PIECE, "Other", None, None, False, True),
]
MILK_RAISE = Decimal("2")
PRICE_FACTOR = {"abhigyan": 1.0, "vij": 1.05, "gopal": 0.95}


def _price(base, key):
    # Round to the nearest 50 paise, like a real price list.
    return money(round(base * PRICE_FACTOR[key] * 2) / 2)


def seed(t):
    biz = t.business
    since = at(history_start() - timedelta(days=1), 0)
    raise_at = at(this_month(), 0)
    categories = {
        name: ProductCategory.objects.create(business=biz, name=name)
        for name in sorted({p[3] for p in PRODUCTS})
    }
    t.products = {}
    for sku, name, unit, cat, sell, cost, track, open_price in PRODUCTS:
        product = Product.objects.create(
            business=biz, sku=sku, name=name, unit=unit,
            category=categories[cat], track_stock=track, allow_open_price=open_price,
        )
        t.products[sku] = product
        if sell is None:
            continue
        is_milk = sku.endswith("-MILK")
        # Backdated history: the price service refuses past dates on purpose,
        # so these setup rows are inserted directly.
        ProductPrice.objects.create(
            business=biz, product=product, price_type="SELLING",
            unit_price=_price(sell, t.key), effective_from=since,
            effective_to=raise_at if is_milk else None, created_by=t.owner,
        )
        if is_milk:
            ProductPrice.objects.create(
                business=biz, product=product, price_type="SELLING",
                unit_price=_price(sell, t.key) + MILK_RAISE,
                effective_from=raise_at, created_by=t.owner,
            )
        ProductPrice.objects.create(
            business=biz, product=product, price_type="PURCHASE",
            unit_price=_price(cost, t.key), effective_from=since, created_by=t.owner,
        )

    change_product_price(
        business_id=biz.pk, product_id=t.products["PANEER"].pk,
        price_type="SELLING", unit_price=_price(380, t.key),
        effective_from=timezone.now() + timedelta(days=7), user=t.owner,
    )
    set_milk_config(
        business_id=biz.pk, cow_product_id=t.products["COW-MILK"].pk,
        buffalo_product_id=t.products["BUF-MILK"].pk, user=t.owner,
    )
