"""
Shop counter sales.

Global lock order for every writer (no deadlocks):
    business (no_key, only to allocate numbers / serialize client_ref)
    -> customer (no_key) -> sale -> product rows (sorted by id)
Billing takes customer -> bill, so it never needs the business lock.
"""
import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.billing.guards import assert_period_open
from apps.billing.money import to_money
from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import require_business_membership
from apps.customers.models import Customer
from apps.deliveries.services import (
    CLOCK_SKEW,
    STAFF_BACKDATE_LIMIT,
    _effective_selling_price,
    _validate_open_price,
    _validate_quantity,
)
from apps.inventory.models import StockMovement
from apps.inventory.services import stock_balance
from apps.payments.services import ClientRefConflict
from apps.products.models import Product
from apps.purchases.milk_queries import local_day_bounds
from apps.sales.models import Sale, SaleLine, line_total_for

MANAGE_ROLES = [BusinessMembership.Role.OWNER, BusinessMembership.Role.MANAGER]
MAX_LINES = 50
PRODUCT_LINE_KEYS = {"product_id", "quantity", "unit_price"}
OTHER_LINE_KEYS = {"description", "amount"}


# ---------------------------------------------------------------- helpers

def _clean_reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    return reason


def _lock_business(business_id):
    try:
        return Business.objects.select_for_update(no_key=True).get(
            pk=business_id, is_active=True, is_deleted=False
        )
    except Business.DoesNotExist as exc:
        raise ValidationError(
            {"business": "The business does not exist or is inactive."}
        ) from exc


def _lock_customer(business, customer_id, *, require_active=True):
    manager = Customer.objects if require_active else Customer._base_manager
    filters = {"pk": customer_id, "business": business}
    if require_active:
        filters.update(is_active=True, is_deleted=False)
    try:
        return manager.select_for_update(no_key=True).get(**filters)
    except Customer.DoesNotExist as exc:
        raise ValidationError({
            "customer": (
                "The customer does not exist, is inactive, "
                "or belongs to another business."
            )
        }) from exc


def _lock_products(business, product_ids, *, require_active=True):
    """Lock products in id order. Returns {product_id: Product}."""
    ids = sorted({str(i) for i in product_ids})
    if not ids:
        return {}
    manager = Product.objects if require_active else Product._base_manager
    filters = {"pk__in": ids, "business": business}
    if require_active:
        filters.update(is_active=True, is_deleted=False)
    found = {
        str(p.pk): p
        for p in manager.select_for_update().filter(**filters).order_by("pk")
    }
    missing = [i for i in ids if i not in found]
    if missing:
        raise ValidationError({
            "lines": (
                "A product does not exist, is inactive, or belongs to "
                "another business."
            )
        })
    return found


def _clean_sold_at(sold_at):
    if sold_at is None:
        return timezone.now()
    if not isinstance(sold_at, datetime):
        raise ValidationError({"sold_at": "A valid timestamp is required."})
    if not timezone.is_aware(sold_at):
        raise ValidationError(
            {"sold_at": "The timestamp must include timezone information."}
        )
    return sold_at.astimezone(timezone.get_current_timezone())


def _clean_lines(lines):
    """
    Shape check, no database. A line is either a product line
    {product_id, quantity, unit_price?} or an other-item line
    {description, amount}; mixing the two shapes is an error.
    """
    if not isinstance(lines, (list, tuple)) or not lines:
        raise ValidationError({"lines": "At least one line is required."})
    if len(lines) > MAX_LINES:
        raise ValidationError({"lines": f"At most {MAX_LINES} lines per sale."})

    clean = []
    for i, line in enumerate(lines, start=1):
        if not isinstance(line, dict):
            raise ValidationError({"lines": f"Line {i} must be an object."})
        keys = set(line)
        if "product_id" in keys:
            extra = keys - PRODUCT_LINE_KEYS
            if extra or not line.get("product_id"):
                raise ValidationError({
                    "lines": (
                        f"Line {i}: a product line takes only product_id, "
                        "quantity and unit_price."
                    )
                })
            clean.append({
                "product_id": str(line["product_id"]),
                "quantity": line.get("quantity"),
                "unit_price": line.get("unit_price"),
            })
        else:
            extra = keys - OTHER_LINE_KEYS
            description = str(line.get("description") or "").strip()
            if extra or not description or "amount" not in keys:
                raise ValidationError({
                    "lines": (
                        f"Line {i}: an other item needs exactly a "
                        "description and an amount."
                    )
                })
            if len(description) > 200:
                raise ValidationError(
                    {"lines": f"Line {i}: description is too long."}
                )
            try:
                amount = to_money(line["amount"], "amount")
            except ValidationError as exc:
                raise ValidationError({"lines": f"Line {i}: {exc.messages[0]}"})
            clean.append({"description": description, "amount": amount})
    return clean


def _request_signature(lines):
    """Normalized request lines, for the client_ref replay comparison."""
    sig = []
    for line in lines:
        if "product_id" in line:
            price = line["unit_price"]
            sig.append((
                line["product_id"],
                Decimal(str(line["quantity"])),
                None if price is None else Decimal(str(price)),
            ))
        else:
            sig.append((None, line["description"], line["amount"]))
    return sig


def _sale_signature(sale):
    sig = []
    for line in sale.lines.all():
        if line.product_id:
            sig.append((str(line.product_id), line.quantity, line.unit_price))
        else:
            sig.append((None, line.description, line.unit_price))
    return sig


def _same_request(sale, *, customer_id, payment_mode, sold_at, notes, lines):
    mine, theirs = _sale_signature(sale), _request_signature(lines)
    if len(mine) != len(theirs):
        return False
    for have, want in zip(mine, theirs):
        if have[0] != want[0] or have[1] != want[1]:
            return False
        if want[2] is not None and have[2] != want[2]:
            return False
    return (
        (str(sale.customer_id) if sale.customer_id else None)
        == (str(customer_id) if customer_id else None)
        and sale.payment_mode == payment_mode
        and sale.notes == notes
        and (sold_at is None or sale.sold_at == sold_at)
    )


def _next_sale_number(business, sold_at):
    day = sold_at.astimezone(ZoneInfo(business.timezone)).strftime("%y%m%d")
    prefix = f"SHOP-{day}-"
    seqs = [
        int(m.group(1))
        for n in Sale.objects.filter(
            business=business, sale_number__startswith=prefix
        ).values_list("sale_number", flat=True)
        if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", n))
    ]
    return f"{prefix}{max(seqs, default=0) + 1}"


# ------------------------------------------------------------- create

def _insert_sale(
    *, business, customer, products, user, role, payment_mode, lines,
    sold_at, client_ref, notes, replaces=None, enforce_window=True,
):
    """Write a sale. Caller holds business, customer and product locks."""
    if payment_mode == Sale.PaymentMode.CREDIT:
        # Customer lock is held: a bill cannot be finalized under us.
        assert_period_open(business, customer.pk, sold_at)

    if enforce_window:
        now = timezone.now()
        if sold_at > now + CLOCK_SKEW:
            raise ValidationError(
                {"sold_at": "A sale cannot be recorded in the future."}
            )
        if (
            role == BusinessMembership.Role.STAFF
            and sold_at < now - STAFF_BACKDATE_LIMIT
        ):
            raise ValidationError({
                "sold_at": "Staff can only record sales from the last 48 hours."
            })

    rows = []
    available = {}
    for position, line in enumerate(lines, start=1):
        if "product_id" not in line:
            rows.append(SaleLine(
                business=business, position=position, product=None,
                description=line["description"], unit="",
                quantity=Decimal("1.000"), unit_price=line["amount"],
            ))
            continue

        product = products[line["product_id"]]
        quantity = _validate_quantity(line["quantity"], product)
        price = line["unit_price"]
        if product.allow_open_price:
            price = _validate_open_price(price)
        elif price is not None:
            raise ValidationError({
                "lines": (
                    f"Line {position}: the price of {product.name} "
                    "cannot be set manually."
                )
            })
        else:
            price = _effective_selling_price(business, product, sold_at)
        if price <= 0:
            raise ValidationError(
                {"lines": f"Line {position}: the price must be above zero."}
            )

        shortfall = Decimal("0.000")
        if product.track_stock:
            if product.pk not in available:
                available[product.pk] = stock_balance(
                    business_id=business.pk, product_id=product.pk
                )
            left = max(available[product.pk], Decimal("0"))
            shortfall = max(quantity - left, Decimal("0"))
            available[product.pk] -= quantity

        rows.append(SaleLine(
            business=business, position=position, product=product,
            description=product.name, unit=product.unit, quantity=quantity,
            unit_price=price, stock_shortfall=shortfall,
        ))

    total = sum((line_total_for(r.quantity, r.unit_price) for r in rows),
                Decimal("0.00"))

    try:
        with transaction.atomic():  # savepoint keeps the outer txn usable
            sale = Sale.objects.create(
                business=business,
                sale_number=_next_sale_number(business, sold_at),
                customer=customer,
                payment_mode=payment_mode,
                sold_at=sold_at,
                total_amount=total,
                client_ref=client_ref or None,
                replaces=replaces,
                notes=notes,
                created_by=user,
            )
    except IntegrityError as exc:
        raise ValidationError(
            "A conflicting sale or client reference was just recorded. "
            "Refresh and retry with a new reference."
        ) from exc

    for row in rows:
        row.sale = sale
        row.save()
        if row.product_id and row.product.track_stock:
            StockMovement.objects.create(
                business=business,
                product=row.product,
                movement_type=StockMovement.MovementType.SALE,
                quantity=row.quantity,
                occurred_at=sold_at,
                reason=f"Shop sale {sale.sale_number}",
                created_by=user,
                sale_line=row,
            )
    return sale


@transaction.atomic
def create_sale(
    *, business_id, user, payment_mode, lines, client_ref,
    customer_id=None, sold_at=None, notes="",
):
    """
    Record a counter sale (any member). CREDIT needs a customer and later
    lands on that customer's monthly bill; CASH / ONLINE are shop history
    only and never touch the customer ledger.

    Retrying the same client_ref returns the original sale; the same ref
    with different data raises ClientRefConflict. A stock shortfall is
    stored on the line, never blocks the sale.
    """
    membership = require_business_membership(user=user, business_id=business_id)

    if payment_mode not in Sale.PaymentMode.values:
        raise ValidationError({"payment_mode": "Invalid payment mode."})
    client_ref = (client_ref or "").strip()
    if not client_ref or len(client_ref) > 64:
        raise ValidationError(
            {"client_ref": "A client_ref of 1 to 64 characters is required."}
        )
    notes = notes or ""
    explicit_sold_at = None if sold_at is None else _clean_sold_at(sold_at)
    lines = _clean_lines(lines)
    if payment_mode == Sale.PaymentMode.CREDIT and not customer_id:
        raise ValidationError(
            {"customer_id": "A credit sale needs a customer."}
        )

    business = _lock_business(business_id)
    customer = _lock_customer(business, customer_id) if customer_id else None
    products = _lock_products(
        business, [l["product_id"] for l in lines if "product_id" in l]
    )

    replay = Sale.objects.filter(business=business, client_ref=client_ref).first()
    if replay is not None:
        if not _same_request(
            replay, customer_id=customer_id, payment_mode=payment_mode,
            sold_at=explicit_sold_at, notes=notes, lines=lines,
        ):
            raise ClientRefConflict({
                "client_ref": (
                    "This reference was already used for a different sale. "
                    "Use a new reference for a new sale."
                )
            })
        return replay

    return _insert_sale(
        business=business, customer=customer, products=products, user=user,
        role=membership.role, payment_mode=payment_mode, lines=lines,
        sold_at=explicit_sold_at or timezone.now(), client_ref=client_ref,
        notes=notes,
    )


# ------------------------------------------------- cancel / correct

def _lock_sale(business_id, sale_id, *, extra_product_ids=(), new_customer_id=None):
    """
    Lock for cancel/correct in the global order. Returns
    (business, customer, sale, products); products cover the sale's own
    stock lines plus extra_product_ids, locked together in id order.
    """
    try:
        # customer and lines of a sale never change, so peeking is safe.
        customer_id = Sale.objects.filter(
            pk=sale_id, business_id=business_id
        ).values_list("customer_id", flat=True).get()
    except Sale.DoesNotExist as exc:
        raise ValidationError({"sale": "Sale not found."}) from exc

    business = _lock_business(business_id)
    customer = None
    if customer_id:
        customer = _lock_customer(business, customer_id, require_active=False)
    elif new_customer_id:
        customer = _lock_customer(business, new_customer_id)

    sale = Sale.objects.select_for_update().get(pk=sale_id, business=business)
    own = list(
        sale.lines.filter(product__isnull=False).values_list("product_id", flat=True)
    )
    products = _lock_products(
        business, own + list(extra_product_ids), require_active=False
    )
    return business, customer, sale, products


def _cancel_locked(sale, *, business, user, reason):
    if sale.status == Sale.Status.CANCELLED:
        raise ValidationError("This sale is already cancelled.")

    if sale.payment_mode == Sale.PaymentMode.CREDIT:
        assert_period_open(business, sale.customer_id, sale.sold_at)

    for original in StockMovement.objects.filter(
        sale_line__sale=sale, movement_type=StockMovement.MovementType.SALE
    ):
        StockMovement.objects.create(
            business_id=sale.business_id,
            product_id=original.product_id,
            movement_type=StockMovement.MovementType.REVERSAL_IN,
            quantity=original.quantity,
            # Same business time as the original so the day nets to zero.
            occurred_at=original.occurred_at,
            reason=f"Cancelled sale {sale.sale_number}: {reason}",
            created_by=user,
            sale_line=original.sale_line,
            reversal_of=original,
        )

    sale.status = Sale.Status.CANCELLED
    sale.cancelled_at = timezone.now()
    sale.cancelled_by = user
    sale.cancel_reason = reason
    sale.save(update_fields=[
        "status", "cancelled_at", "cancelled_by", "cancel_reason",
    ])


@transaction.atomic
def cancel_sale(*, business_id, sale_id, user, reason):
    """Cancel a sale (owner/manager); keeps the row and reverses stock."""
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=MANAGE_ROLES
    )
    reason = _clean_reason(reason)
    business, _customer, sale, _products = _lock_sale(business_id, sale_id)
    _cancel_locked(sale, business=business, user=user, reason=reason)
    return sale


@transaction.atomic
def correct_sale(
    *, business_id, sale_id, user, reason, payment_mode, lines,
    customer_id=None, notes="",
):
    """
    Cancel a sale and record the replacement at the original time (owner /
    manager). The customer of a sale cannot change; a sale that had none
    may be given one (e.g. cash turned into credit).
    """
    membership = require_business_membership(
        user=user, business_id=business_id, allowed_roles=MANAGE_ROLES
    )
    reason = _clean_reason(reason)
    if payment_mode not in Sale.PaymentMode.values:
        raise ValidationError({"payment_mode": "Invalid payment mode."})
    lines = _clean_lines(lines)
    new_ids = [l["product_id"] for l in lines if "product_id" in l]

    business, customer, old, products = _lock_sale(
        business_id, sale_id, extra_product_ids=new_ids,
        new_customer_id=customer_id,
    )
    if old.customer_id and customer_id and str(customer_id) != str(old.customer_id):
        raise ValidationError(
            {"customer_id": "The customer of a sale cannot be changed."}
        )
    if payment_mode == Sale.PaymentMode.CREDIT and customer is None:
        raise ValidationError({"customer_id": "A credit sale needs a customer."})
    notes = notes or ""

    # Active-only products for the replacement (the lock above allowed old
    # inactive ones so an old sale can still be cancelled).
    for pid in new_ids:
        p = products[pid]
        if not p.is_active or p.is_deleted:
            raise ValidationError({
                "lines": f"{p.name} is inactive and cannot be sold."
            })

    _cancel_locked(old, business=business, user=user, reason=reason)

    return _insert_sale(
        business=business, customer=customer, products=products, user=user,
        role=membership.role, payment_mode=payment_mode, lines=lines,
        sold_at=old.sold_at, client_ref=None, notes=notes, replaces=old,
        enforce_window=False,
    )


# --------------------------------------------------------- customers

@transaction.atomic
def quick_create_customer(*, business_id, user, name, phone):
    """Counter shortcut (any member): name + phone, code CUST-<n> generated."""
    require_business_membership(user=user, business_id=business_id)
    name = (name or "").strip()
    phone = (phone or "").strip()
    if not name or len(name) > 200:
        raise ValidationError({"name": "A name is required."})
    if not phone or len(phone) > 20:
        raise ValidationError({"phone": "A phone number is required."})

    business = _lock_business(business_id)
    pattern = re.compile(r"CUST-(\d+)")
    seqs = [
        int(m.group(1))
        for code in Customer._base_manager.filter(
            business=business, customer_code__startswith="CUST-"
        ).values_list("customer_code", flat=True)
        if (m := pattern.fullmatch(code))
    ]
    return Customer.objects.create(
        business=business,
        customer_code=f"CUST-{max(seqs, default=0) + 1}",
        name=name,
        phone=phone,
    )


# ------------------------------------------------------------ reports

def daily_summary(*, business_id, user, day):
    """Completed sales of one business-local day, totalled per payment mode."""
    membership = require_business_membership(user=user, business_id=business_id)
    business = membership.business
    start, end = local_day_bounds(business, day)
    rows = (
        Sale.objects.filter(
            business=business, status=Sale.Status.COMPLETED,
            sold_at__gte=start, sold_at__lt=end,
        )
        .values("payment_mode")
        .annotate(count=Count("id"), total=Sum("total_amount"))
    )
    by_mode = {m: {"count": 0, "total": Decimal("0.00")}
               for m in Sale.PaymentMode.values}
    for r in rows:
        by_mode[r["payment_mode"]] = {"count": r["count"], "total": r["total"]}
    return {
        "date": day,
        "by_payment_mode": by_mode,
        "count": sum(v["count"] for v in by_mode.values()),
        "total": sum((v["total"] for v in by_mode.values()), Decimal("0.00")),
    }
