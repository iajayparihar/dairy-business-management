from datetime import timedelta

from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.db.models import Sum
from apps.businesses.models import Business
from apps.inventory.models import StockMovement
from apps.inventory.services import milk_product_for, stock_balance
from apps.products.models import Product
from apps.suppliers.models import Supplier, SupplierMilkRate
from apps.purchases.models import MilkCollection, SupplierPurchase, PurchaseLine
from apps.suppliers.services import calculate_supplier_milk_amount
from apps.businesses.services import require_business_membership
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
)
from apps.payments.selectors import get_payment_amounts
from apps.payments.services import allocate_supplier_advance

ALLOWED_ROLES = ["OWNER", "MANAGER"]


def _validate_membership(*, user, business_id):
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=ALLOWED_ROLES,
    )


def _decimal(value, field_name):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError({field_name: "Enter a valid number."})

    if not number.is_finite():
        raise ValidationError({field_name: "Enter a finite number."})

    return number


def _validate_lines(lines):
    if not lines:
        raise ValidationError("A purchase must contain at least one product.")

    validated = []

    for item in lines:
        product_id = item.get("product_id")
        quantity = _decimal(item.get("quantity"), "quantity")
        unit_price = _decimal(item.get("unit_price"), "unit_price")

        if quantity <= 0 or quantity.as_tuple().exponent < -3:
            raise ValidationError(
                {"quantity": "Quantity must be positive with at most 3 decimals."}
            )

        if unit_price < 0 or unit_price.as_tuple().exponent < -4:
            raise ValidationError(
                {
                    "unit_price": (
                        "Price must be non-negative "
                        "with at most 4 decimal places."
                    )
                }
            )

        validated.append(
            {
                "product_id": product_id,
                "quantity": quantity,
                "unit_price": unit_price,
            }
        )

    return validated


def _calculate_total(purchase):
    total = sum(
        (line.line_total for line in purchase.lines.all()),
        Decimal("0.00"),
    )
    purchase.total_amount = total.quantize(Decimal("0.01"))
    purchase.save(update_fields=["total_amount", "updated_at"])


def _not_in_future(moment):
    """Business events are never dated ahead (5 minutes of phone-clock skew allowed)."""
    if moment > timezone.now() + timedelta(minutes=5):
        raise ValidationError({"purchased_at": "The date cannot be in the future."})


@transaction.atomic
def create_purchase(
    *,
    business_id,
    supplier_id,
    purchase_number,
    purchased_at,
    lines,
    user,
    notes="",
):
    _validate_membership(user=user, business_id=business_id)
    validated_lines = _validate_lines(lines)

    if not timezone.is_aware(purchased_at):
        raise ValidationError(
            {"purchased_at": "Purchase date and time must include a timezone."}
        )

    _not_in_future(purchased_at)

    business = Business.objects.get(pk=business_id)
    supplier = Supplier.objects.get(
        pk=supplier_id,
        business=business,
        is_active=True,
    )

    purchase = SupplierPurchase.objects.create(
        business=business,
        supplier=supplier,
        purchase_number=purchase_number,
        purchased_at=purchased_at,
        notes=notes,
        created_by=user,
    )

    for item in validated_lines:
        product = Product.objects.get(
            pk=item["product_id"],
            business=business,
            is_active=True,
        )

        PurchaseLine.objects.create(
            purchase=purchase,
            business=business,
            product=product,
            quantity=item["quantity"],
            unit_price=item["unit_price"],
        )

    _calculate_total(purchase)
    return purchase


def _lock_purchase(purchase_id, business_id):
    """Lock a purchase row, scoped to the business when one is given."""
    scope = {} if business_id is None else {"business_id": business_id}
    return SupplierPurchase.objects.select_for_update().get(
        pk=purchase_id, **scope
    )


@transaction.atomic
def update_draft_purchase(*, purchase_id, user, supplier_id=None,
                        purchased_at=None, lines=None, notes=None,
                        business_id=None):
    purchase = _lock_purchase(purchase_id, business_id)

    _validate_membership(
        user=user,
        business_id=purchase.business_id,
    )

    if purchase.status != SupplierPurchase.Status.DRAFT:
        raise ValidationError("Only draft purchases can be edited.")

    if supplier_id is not None:
        purchase.supplier = Supplier.objects.get(
            pk=supplier_id,
            business=purchase.business,
            is_active=True,
        )

    if purchased_at is not None:
        if not timezone.is_aware(purchased_at):
            raise ValidationError(
                {"purchased_at": "Purchase date and time must include a timezone."}
            )
        _not_in_future(purchased_at)
        purchase.purchased_at = purchased_at

    if notes is not None:
        purchase.notes = notes

    purchase.save()

    if lines is not None:
        validated_lines = _validate_lines(lines)

        for old_line in purchase.lines.all():
            old_line.soft_delete(user=user)

        for item in validated_lines:
            product = Product.objects.get(
                pk=item["product_id"],
                business=purchase.business,
                is_active=True,
            )

            PurchaseLine.objects.create(
                purchase=purchase,
                business=purchase.business,
                product=product,
                quantity=item["quantity"],
                unit_price=item["unit_price"],
            )

    _calculate_total(purchase)
    return purchase


@transaction.atomic
def receive_purchase(*, purchase_id, user, business_id=None):
    purchase = _lock_purchase(purchase_id, business_id)

    _validate_membership(
        user=user,
        business_id=purchase.business_id,
    )

    if purchase.status != SupplierPurchase.Status.DRAFT:
        raise ValidationError("Only draft purchases can be received.")

    lines = list(
        purchase.lines.select_related("product").order_by("pk")
    )

    if not lines:
        raise ValidationError("Cannot receive a purchase without product lines.")

    received_at = timezone.now()

    for line in lines:
        if line.business_id != purchase.business_id:
            raise ValidationError("Purchase line belongs to another business.")

        if line.product.business_id != purchase.business_id:
            raise ValidationError("Product belongs to another business.")

        # Inventory is tracked only for products with track_stock enabled.
        if not line.product.track_stock:
            continue

        StockMovement.objects.create(
            business=purchase.business,
            product=line.product,
            movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
            quantity=line.quantity,
            occurred_at=received_at,
            reason=f"Received from purchase {purchase.purchase_number}",
            created_by=user,
            purchase_line=line,
        )

    purchase.status = SupplierPurchase.Status.RECEIVED
    purchase.received_at = received_at
    purchase.save(update_fields=["status", "received_at", "updated_at"])

    return purchase


@transaction.atomic
def cancel_draft_purchase(*, purchase_id, user, reason="", business_id=None):
    """Cancel a never-received draft. No stock or payment is touched."""
    purchase = _lock_purchase(purchase_id, business_id)

    _validate_membership(user=user, business_id=purchase.business_id)

    if purchase.status != SupplierPurchase.Status.DRAFT:
        raise ValidationError("Only draft purchases can be cancelled.")

    purchase.status = SupplierPurchase.Status.CANCELLED
    purchase.cancelled_at = timezone.now()
    purchase.cancelled_by = user
    purchase.cancel_reason = (reason or "").strip()
    if not purchase.cancel_reason:
        raise ValidationError({"reason": "A reason is required."})
    purchase.save(
        update_fields=[
            "status", "cancelled_at", "cancelled_by",
            "cancel_reason", "updated_at",
        ]
    )
    return purchase


class ClientRefConflict(ValidationError):
    """client_ref reused for a different request (API answers 409)."""


MORNING_ENDS_HOUR = 15  # local hour: before this is MORNING


def _local(business, moment):
    return moment.astimezone(ZoneInfo(business.timezone))


def default_shift(business, collected_at):
    """Derived from the entry's own time (never 'now'), so retries match."""
    if _local(business, collected_at).hour < MORNING_ENDS_HOUR:
        return MilkCollection.Shift.MORNING
    return MilkCollection.Shift.EVENING


def next_collection_label(business, supplier, collected_at, shift, milk_type):
    """
    <SUPPLIER_CODE>-<YYMMDD>-<M|E>-<C|B><seq>, e.g. RAM01-261015-M-B2.

    Counts every row ever written for the prefix (cancelled included,
    soft-deleted included) so a number is never reused. Callers hold the
    business row lock; uniq_milk_collection_number is the final guard.
    """
    prefix = "{}-{:%y%m%d}-{}-{}".format(
        supplier.supplier_code,
        _local(business, collected_at),
        "M" if shift == MilkCollection.Shift.MORNING else "E",
        "C" if milk_type == MilkCollection.MilkType.COW else "B",
    )
    used = [
        int(number[len(prefix):])
        for number in MilkCollection.all_objects.filter(
            business=business,
            supplier=supplier,
            collection_number__startswith=prefix,
        ).values_list("collection_number", flat=True)
        if number[len(prefix):].isdigit()
    ]
    return f"{prefix}{max(used, default=0) + 1}"


@transaction.atomic
def create_milk_collection(
    *,
    business_id,
    supplier_id,
    milk_type,
    collected_at,
    litres,
    user,
    fat=None,
    shift=None,
    client_ref=None,
    replaces=None,
):
    """
    Record one can of milk. Returns the collection; `.replayed` is True when
    client_ref matched an identical earlier request (nothing new created).
    """
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=["OWNER", "MANAGER"],
    )

    if not timezone.is_aware(collected_at):
        raise ValidationError({
            "collected_at": "Collection time must include a timezone."
        })

    # Same rule as expenses: never in the future (5 minutes of phone-clock skew allowed).
    if collected_at > timezone.now() + timedelta(minutes=5):
        raise ValidationError({
            "collected_at": "Collection time cannot be in the future."
        })

    if milk_type not in MilkCollection.MilkType.values:
        raise ValidationError({"milk_type": "Invalid milk type."})

    if shift is not None and shift not in MilkCollection.Shift.values:
        raise ValidationError({"shift": "Invalid shift."})

    client_ref = (client_ref or "").strip() or None

    def decimal_value(value, field, minimum, max_places):
        from decimal import Decimal, InvalidOperation

        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError({field: "Enter a valid number."})

        if not number.is_finite() or number < minimum:
            raise ValidationError({field: "Invalid value."})

        if number.as_tuple().exponent < -max_places:
            raise ValidationError({
                field: f"Maximum {max_places} decimal places allowed."
            })

        return number

    litres = decimal_value(litres, "litres", Decimal("0.001"), 3)

    # Cow milk is flat per litre: fat is not recorded. Buffalo needs fat.
    if milk_type == MilkCollection.MilkType.COW:
        if fat is not None:
            raise ValidationError({
                "fat": "Fat is not recorded for cow milk."
            })
    else:
        if fat is None:
            raise ValidationError({
                "fat": "Fat is required for buffalo milk."
            })
        fat = decimal_value(fat, "fat", Decimal("0.01"), 2)
        # Range from .env (MILK_FAT_MIN / MILK_FAT_MAX): catches 45 typed for 4.5.
        # A litres-only correction keeps the recorded fat, so it is not re-checked.
        if replaces is None or fat != replaces.fat:
            low, high = settings.MILK_FAT_MIN, settings.MILK_FAT_MAX
            if not low <= fat <= high:
                raise ValidationError({
                    "fat": f"Fat must be between {low} and {high}. Check the decimal point."
                })

    # Serialize collection creation within the business (also makes the
    # label sequence and the client_ref replay check race-free).
    # NO KEY UPDATE still excludes other creators, but unlike a plain
    # FOR UPDATE it does not block every INSERT that merely references
    # the business (payments, allocations, stock), which would deadlock
    # against writers holding a purchase/payment lock.
    # The unique database constraints remain the final safeguard.
    business = Business.objects.select_for_update(no_key=True).get(
        pk=business_id
    )

    shift = shift or default_shift(business, collected_at)

    if client_ref:
        replay = MilkCollection.all_objects.filter(
            business=business,
            client_ref=client_ref,
        ).first()

        if replay is not None:
            same = (
                replay.supplier_id == supplier_id
                and replay.milk_type == milk_type
                and replay.litres == litres
                and replay.fat == (fat if fat is not None else Decimal("0"))
                and replay.collected_at == collected_at
                and replay.shift == shift
            )

            if not same:
                raise ClientRefConflict({
                    "client_ref": (
                        "This reference was already used for a different "
                        "milk entry. Use a new reference for a new entry."
                    )
                })

            replay.replayed = True
            return replay

    supplier = Supplier.objects.filter(
        pk=supplier_id,
        business_id=business.id,
        is_active=True,
        is_deleted=False,
    ).first()

    if supplier is None:
        raise ValidationError(
            "Supplier does not exist, is inactive, or belongs to another business."
        )

    collection_number = next_collection_label(
        business, supplier, collected_at, shift, milk_type,
    )

    # Prefix leaves room for the purchase number's 50-character limit.
    purchase_number = f"MILK-{collection_number}"

    if len(purchase_number) > 50:
        raise ValidationError({
            "supplier": "Supplier code is too long to build a milk label."
        })

    if SupplierPurchase.objects.filter(
        business=business,
        purchase_number=purchase_number,
    ).exists():
        raise ValidationError(
            "A purchase with this milk label already exists."
        )

    calculation = calculate_supplier_milk_amount(
        business_id=business.id,
        supplier_id=supplier.id,
        milk_type=milk_type,
        collected_at=collected_at,
        litres=litres,
        fat=fat,
    )

    rate_record = calculation["rate_record"]
    amount = calculation["amount"]
    pricing_basis = calculation["pricing_basis"]

    # Milk collection creates a payable, not a stock-tracked
    # product purchase. Therefore no PurchaseLine is created.
    purchase = SupplierPurchase.objects.create(
        business=business,
        supplier=supplier,
        purchase_number=purchase_number,
        purchased_at=collected_at,
        status=SupplierPurchase.Status.RECEIVED,
        notes=f"Milk collection {collection_number}",
        total_amount=amount,
        created_by=user,
        received_at=timezone.now(),
    )

    collection = MilkCollection.objects.create(
        business=business,
        supplier=supplier,
        rate_record=rate_record,
        collection_number=collection_number,
        milk_type=milk_type,
        collected_at=collected_at,
        shift=shift,
        client_ref=client_ref,
        replaces=replaces,
        litres=litres,
        fat=fat if fat is not None else Decimal("0"),
        pricing_basis=pricing_basis,
        applied_rate=rate_record.rate,
        amount=amount,
        purchase=purchase,
        created_by=user,
    )

    # Raw milk enters stock when a product is mapped for this milk type.
    # With no mapping the collection still succeeds (stock_posted=False).
    milk_product = milk_product_for(business.id, milk_type)

    if milk_product is not None:
        StockMovement.objects.create(
            business=business,
            product=milk_product,
            movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
            quantity=litres,
            occurred_at=collected_at,
            reason=f"Milk collection {collection_number}",
            created_by=user,
            milk_collection=collection,
        )

    # Automatically apply available supplier advances to this purchase.
    remaining_amount = purchase.total_amount

    advance_payments = SupplierPayment.objects.filter(
        business=business,
        supplier=supplier,
    ).order_by("paid_at", "created_at")

    for payment in advance_payments:
        if remaining_amount <= Decimal("0.00"):
            break

        # Net of reversals and of allocations on live purchases.
        available_advance = get_payment_amounts(payment)["unallocated"]

        if available_advance <= Decimal("0.00"):
            continue

        allocation_amount = min(
            remaining_amount,
            available_advance,
        )

        allocate_supplier_advance(
            payment_id=payment.pk,
            purchase_id=purchase.pk,
            amount=allocation_amount,
            user=user,
        )

        remaining_amount -= allocation_amount

    collection.replayed = False
    return collection


# ---------------------------------------------------------------
# Cancel / correct a milk entry
#
# Lock order (every writer, so none can deadlock):
#     business (NO KEY UPDATE) -> purchase -> collection -> product
# allocate_supplier_advance takes purchase -> payment, so a payment can
# never be allocated to a purchase that is being cancelled: whoever gets
# the purchase lock first wins, and the other sees the final status.
# ---------------------------------------------------------------

MIN_REASON_LENGTH = 3


def _clean_reason(reason):
    reason = (reason or "").strip()

    if len(reason) < MIN_REASON_LENGTH:
        raise ValidationError({
            "reason": "Give a reason (at least 3 characters)."
        })

    return reason


def _lock_collection(business_id, collection_id):
    """Lock purchase then collection; raises MilkCollection.DoesNotExist."""
    # The purchase link never changes, so reading it unlocked is safe.
    purchase_id = MilkCollection.all_objects.filter(
        pk=collection_id, business_id=business_id,
    ).values_list("purchase_id", flat=True).get()

    purchase = SupplierPurchase.objects.select_for_update().get(
        pk=purchase_id, business_id=business_id,
    )
    collection = MilkCollection.objects.select_for_update().get(
        pk=collection_id, business_id=business_id,
    )
    return collection, purchase


def _cancel_locked(collection, purchase, *, user, reason):
    """Cancel an already-locked entry: reverse stock, flag any shortfall."""
    if collection.status == MilkCollection.Status.CANCELLED:
        raise ValidationError("This milk entry is already cancelled.")

    now = timezone.now()
    shortfall = Decimal("0.000")

    original = StockMovement.objects.filter(
        milk_collection=collection,
        movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
    ).first()

    if original is not None:
        # Product is the last lock in the order.
        Product.objects.select_for_update().get(pk=original.product_id)

        StockMovement.objects.create(
            business_id=collection.business_id,
            product_id=original.product_id,
            movement_type=StockMovement.MovementType.REVERSAL_OUT,
            quantity=original.quantity,
            # Same business time as the receipt, so that day nets to zero.
            occurred_at=original.occurred_at,
            reason=f"Cancelled milk entry {collection.collection_number}: "
                   f"{reason}",
            created_by=user,
            milk_collection=collection,
            reversal_of=original,
        )

        # Milk already delivered: allow, but record how much is missing.
        balance = stock_balance(
            business_id=collection.business_id,
            product_id=original.product_id,
        )
        if balance < 0:
            shortfall = min(-balance, original.quantity)

    collection.status = MilkCollection.Status.CANCELLED
    collection.cancelled_at = now
    collection.cancelled_by = user
    collection.cancel_reason = reason
    collection.cancel_stock_shortfall = shortfall
    collection.save(update_fields=[
        "status", "cancelled_at", "cancelled_by", "cancel_reason",
        "cancel_stock_shortfall",
    ])

    # Any payment allocated to it flows back to the supplier's credit:
    # balances and the allocator ignore allocations on cancelled purchases.
    purchase.status = SupplierPurchase.Status.CANCELLED
    purchase.cancelled_at = now
    purchase.cancelled_by = user
    purchase.cancel_reason = reason
    purchase.save(update_fields=[
        "status", "cancelled_at", "cancelled_by", "cancel_reason",
        "updated_at",
    ])


@transaction.atomic
def cancel_milk_collection(*, business_id, collection_id, user, reason):
    """Cancel a milk entry (owner/manager). The row is kept for audit."""
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=ALLOWED_ROLES,
    )
    reason = _clean_reason(reason)

    collection, purchase = _lock_collection(business_id, collection_id)
    _cancel_locked(collection, purchase, user=user, reason=reason)
    return collection


@transaction.atomic
def correct_milk_collection(
    *,
    business_id,
    collection_id,
    user,
    reason,
    litres,
    fat=None,
    reprice=False,
):
    """
    Correct litres / fat: cancel the entry and record a replacement at the
    ORIGINAL time and shift, so the rate in force then still applies.
    Returns the replacement; `.replaces` is the cancelled entry. One
    transaction: any failure leaves everything as it was.
    """
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=ALLOWED_ROLES,
    )
    reason = _clean_reason(reason)

    # business lock first (create_milk_collection re-takes it, no-op).
    Business.objects.select_for_update(no_key=True).get(pk=business_id)
    old, purchase = _lock_collection(business_id, collection_id)

    if old.status == MilkCollection.Status.CANCELLED:
        raise ValidationError("This milk entry is already cancelled.")

    if old.milk_type == MilkCollection.MilkType.BUFFALO and fat is None:
        fat = old.fat  # litres-only correction keeps the recorded fat

    if (
        not reprice  # a rate correction re-prices an entry whose litres and fat stay as they are
        and Decimal(str(litres)) == old.litres
        and (fat is None or Decimal(str(fat)) == old.fat)
    ):
        raise ValidationError("Nothing to correct.")

    _cancel_locked(
        old, purchase, user=user, reason=f"Corrected: {reason}",
    )

    new = create_milk_collection(
        business_id=business_id,
        supplier_id=old.supplier_id,
        milk_type=old.milk_type,
        collected_at=old.collected_at,
        shift=old.shift,
        litres=litres,
        fat=fat,
        user=user,
        replaces=old,
    )

    # The shortfall that matters is the one left after the replacement
    # is posted (cancel alone briefly removes the whole old quantity).
    receipt = StockMovement.objects.filter(
        milk_collection=old,
        movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
    ).first()
    if receipt is not None:
        balance = stock_balance(
            business_id=business_id, product_id=receipt.product_id,
        )
        removed = max(old.litres - new.litres, Decimal("0"))
        old.cancel_stock_shortfall = min(max(-balance, Decimal("0")), removed)
        old.save(update_fields=["cancel_stock_shortfall"])

    return new
