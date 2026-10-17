from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.billing.guards import assert_period_open
from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import require_business_membership
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.inventory.models import StockMovement
from apps.inventory.services import STOCK_IN_TYPES, STOCK_OUT_TYPES
from apps.products.models import Product, ProductPrice

# Delivery boys (STAFF) may only record recent deliveries; owners and
# managers may backdate. Nobody may record a delivery in the future.
STAFF_BACKDATE_LIMIT = timedelta(hours=48)
CLOCK_SKEW = timedelta(minutes=5)
WHOLE_NUMBER_UNITS = {Product.Unit.PIECE, Product.Unit.PACKET}


@transaction.atomic
def create_delivery(
    *,
    business_id,
    customer_id,
    product_id,
    quantity,
    user,
    delivered_at=None,
    shift=Delivery.Shift.MORNING,
    unit_price=None,
    client_ref=None,
    notes="",
    replaces=None,
    visit=None,
):
    """
    Record one product delivered to one customer in one shift.

    delivered_at defaults to now, must be timezone-aware and not in the
    future. The selling price effective at that exact timestamp is saved
    as a historical snapshot. unit_price is accepted only for open-price
    products; for everything else the client cannot choose prices.

    Re-sending the same client_ref returns the original delivery, so
    retries from a poor connection never double-charge.

    If stock tracking is on and stock is short, the delivery is still
    recorded (the milk has left) and the gap is stored in
    stock_shortfall for reconciliation.
    """

    membership = require_business_membership(
        user=user,
        business_id=business_id,
    )

    if delivered_at is None:
        delivered_at = timezone.now()

    if not isinstance(delivered_at, datetime):
        raise ValidationError({
            "delivered_at": "A valid delivery timestamp is required."
        })

    if not timezone.is_aware(delivered_at):
        raise ValidationError({
            "delivered_at": (
                "The delivery timestamp must include timezone information."
            )
        })

    delivered_at = delivered_at.astimezone(timezone.get_current_timezone())

    if shift not in Delivery.Shift.values:
        raise ValidationError({"shift": "Invalid shift."})

    try:
        business = Business.objects.get(
            pk=business_id,
            is_active=True,
            is_deleted=False,
        )
    except Business.DoesNotExist as exc:
        raise ValidationError({
            "business": "The business does not exist or is inactive."
        }) from exc

    try:
        # Lock order: customer -> product. The customer lock keeps a bill
        # from being finalized between the billed-period check and the insert.
        customer = Customer.objects.select_for_update(no_key=True).get(
            pk=customer_id,
            business=business,
            is_active=True,
            is_deleted=False,
        )
    except Customer.DoesNotExist as exc:
        raise ValidationError({
            "customer": (
                "The customer does not exist, is inactive, "
                "or belongs to another business."
            )
        }) from exc

    # The product lock serializes, per product, the duplicate check, the
    # client_ref replay and the stock check below. Anything it cannot cover
    # (e.g. one client_ref reused across products) is caught by the unique
    # constraints and reported at the insert.
    try:
        product = Product.objects.select_for_update().get(
            pk=product_id,
            business=business,
            is_active=True,
            is_deleted=False,
        )
    except Product.DoesNotExist as exc:
        raise ValidationError({
            "product": (
                "The product does not exist, is inactive, "
                "or belongs to another business."
            )
        }) from exc

    quantity = _validate_quantity(quantity, product)

    # A retry must be answered with the original delivery even if its
    # timestamp is now outside the allowed window, so replay comes first.
    if client_ref:
        replay = Delivery.objects.filter(
            business=business,
            client_ref=client_ref,
        ).first()

        if replay is not None:
            same = (
                replay.customer_id == customer.pk
                and replay.product_id == product.pk
                and replay.quantity == quantity
                and replay.shift == shift
                and (unit_price is None or replay.unit_price == Decimal(str(unit_price)))
            )

            if not same:
                raise ValidationError({
                    "client_ref": (
                        "This reference was already used for a different "
                        "delivery. Use a new reference for a new entry."
                    )
                })
            return replay

    assert_period_open(business, customer.pk, delivered_at)

    now = timezone.now()

    if delivered_at > now + CLOCK_SKEW:
        raise ValidationError({
            "delivered_at": "A delivery cannot be recorded in the future."
        })

    if (
        membership.role == BusinessMembership.Role.STAFF
        and delivered_at < now - STAFF_BACKDATE_LIMIT
    ):
        raise ValidationError({
            "delivered_at": (
                "Staff can only record deliveries from the last 48 hours."
            )
        })

    if Delivery.objects.filter(
        business=business,
        customer=customer,
        product=product,
        delivery_date=delivered_at.date(),
        shift=shift,
        status=Delivery.Status.DELIVERED,
    ).exists():
        raise ValidationError(
            "A delivery of this product is already recorded for this "
            "customer, date and shift. Cancel it to record a correction."
        )

    if product.allow_open_price:
        unit_price = _validate_open_price(unit_price)
    elif unit_price is not None:
        raise ValidationError({
            "unit_price": "The price of this product cannot be set manually."
        })
    else:
        unit_price = _effective_selling_price(
            business, product, delivered_at,
        )

    shortfall = Decimal("0.000")

    if product.track_stock:
        available = _get_stock_balance(
            business_id=business.pk,
            product_id=product.pk,
        )
        shortfall = max(
            quantity - max(available, Decimal("0")),
            Decimal("0"),
        )

    try:
        with transaction.atomic():  # savepoint: keeps the outer txn usable
            delivery = Delivery.objects.create(
                business=business,
                customer=customer,
                product=product,
                delivered_at=delivered_at,
                shift=shift,
                quantity=quantity,
                unit_price=unit_price,
                stock_shortfall=shortfall,
                client_ref=client_ref or None,
                notes=notes,
                created_by=user,
                replaces=replaces,
                visit=visit,
            )
    except IntegrityError as exc:
        raise ValidationError(
            "A conflicting delivery or client reference was just recorded. "
            "Refresh and retry with a new reference."
        ) from exc

    if product.track_stock:
        StockMovement.objects.create(
            business=business,
            product=product,
            movement_type=StockMovement.MovementType.DELIVERY,
            quantity=quantity,
            occurred_at=delivered_at,
            reason=f"Customer delivery {delivery.pk}",
            created_by=user,
            delivery=delivery,
        )

    return delivery


def _validate_quantity(quantity, product):
    if quantity is None:
        raise ValidationError({"quantity": "Quantity is required."})

    try:
        quantity = Decimal(str(quantity))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError({
            "quantity": "Enter a valid decimal quantity."
        }) from exc

    if not quantity.is_finite() or quantity <= Decimal("0"):
        raise ValidationError({
            "quantity": "Quantity must be greater than zero."
        })

    if quantity.as_tuple().exponent < -3:
        raise ValidationError({
            "quantity": "Quantity supports at most three decimal places."
        })

    if quantity >= Decimal("1000000000"):
        raise ValidationError({
            "quantity": "Quantity exceeds the supported maximum."
        })

    if (
        product.unit in WHOLE_NUMBER_UNITS
        and quantity != quantity.to_integral_value()
    ):
        raise ValidationError({
            "quantity": f"{product.get_unit_display()} must be a whole number."
        })

    return quantity


def _validate_open_price(unit_price):
    if unit_price is None:
        raise ValidationError({
            "unit_price": "This product requires a price per unit."
        })

    try:
        unit_price = Decimal(str(unit_price))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError({
            "unit_price": "Enter a valid price."
        }) from exc

    if (
        not unit_price.is_finite()
        or unit_price < 0
        or unit_price.as_tuple().exponent < -2
        or unit_price >= Decimal("10000000000")
    ):
        raise ValidationError({
            "unit_price": (
                "Price must be zero or more, with at most 2 decimals."
            )
        })

    return unit_price


def _effective_selling_price(business, product, at):
    """Selling price effective at `at`; periods are [from, to)."""
    price = (
        ProductPrice.objects.filter(
            business=business,
            product=product,
            price_type=ProductPrice.PriceType.SELLING,
            is_deleted=False,
            effective_from__lte=at,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=at))
        .order_by("-effective_from")
        .first()
    )

    if price is None:
        raise ValidationError({
            "product": (
                "No selling price is configured for this product "
                "at the delivery timestamp."
            )
        })

    return price.unit_price


def _get_stock_balance(*, business_id, product_id):
    totals = StockMovement.objects.filter(
        business_id=business_id,
        product_id=product_id,
    ).aggregate(
        stock_in=Sum("quantity", filter=Q(movement_type__in=STOCK_IN_TYPES)),
        stock_out=Sum("quantity", filter=Q(movement_type__in=STOCK_OUT_TYPES)),
    )

    return (totals["stock_in"] or Decimal("0.000")) - (
        totals["stock_out"] or Decimal("0.000")
    )


MANAGE_ROLES = [
    BusinessMembership.Role.OWNER,
    BusinessMembership.Role.MANAGER,
]


def _lock_delivery(business_id, delivery_id):
    """
    Lock a delivery for cancellation/correction; raises Delivery.DoesNotExist.

    Global lock order, used by every writer so none can deadlock:
        route run -> customer -> delivery -> product
    The run lock is what makes "run is CLOSED" a stable fact: close_run,
    reopen_run, record_entry and this function all take it first, so a
    delivery can never be changed in a run that closes underneath it.
    A delivery inside a finalized bill's period is refused here too
    (apps/billing/guards.assert_period_open), after the locks.
    """
    from apps.routes.models import RouteRun

    # A delivery's visit and customer never change, so peeking without a lock
    # is safe.
    visit_id, customer_id = Delivery.objects.filter(
        pk=delivery_id, business_id=business_id,
    ).values_list("visit_id", "customer_id").get()

    run = None
    if visit_id:
        run = RouteRun.objects.select_for_update().get(visits__pk=visit_id)

    # The customer lock (also taken by create_delivery and by bill finalize)
    # makes "this period is billed" a stable fact. _base_manager: a deleted
    # or inactive customer's deliveries can still be cancelled.
    Customer._base_manager.select_for_update(no_key=True).get(pk=customer_id)

    delivery = Delivery.objects.select_for_update().get(
        pk=delivery_id, business_id=business_id,
    )

    if run is not None and run.status == RouteRun.Status.CLOSED:
        raise ValidationError(
            "This delivery belongs to a closed route run. "
            "Reopen the run to correct it."
        )

    assert_period_open(
        Business.objects.only("timezone").get(pk=business_id),
        customer_id,
        delivery.delivered_at,
    )

    # Same lock create_delivery takes, so the stock reversal cannot
    # interleave with a concurrent shortfall calculation.
    Product._base_manager.select_for_update().get(pk=delivery.product_id)

    return delivery


def _clean_reason(reason):
    reason = (reason or "").strip()

    if not reason:
        raise ValidationError({"reason": "A reason is required."})

    return reason


def _cancel_locked(delivery, *, user, reason):
    """Cancel an already-locked delivery and reverse its stock."""
    if delivery.status == Delivery.Status.CANCELLED:
        raise ValidationError("This delivery is already cancelled.")

    now = timezone.now()
    original = StockMovement.objects.filter(
        delivery=delivery,
        movement_type=StockMovement.MovementType.DELIVERY,
    ).first()

    # Stock goes back through a compensating entry; history is kept.
    if original is not None:
        StockMovement.objects.create(
            business_id=delivery.business_id,
            product_id=delivery.product_id,
            movement_type=StockMovement.MovementType.REVERSAL_IN,
            quantity=original.quantity,
            # Same business time as the original, so the delivery date
            # nets to zero; created_at records when it was really posted.
            occurred_at=original.occurred_at,
            reason=f"Cancelled delivery {delivery.pk}: {reason}",
            created_by=user,
            delivery=delivery,
            reversal_of=original,
        )

    delivery.status = Delivery.Status.CANCELLED
    delivery.cancelled_at = now
    delivery.cancelled_by = user
    delivery.cancel_reason = reason
    delivery.save(update_fields=[
        "status", "cancelled_at", "cancelled_by", "cancel_reason",
        "updated_at",
    ])


@transaction.atomic
def cancel_delivery(*, business_id, delivery_id, user, reason):
    """Cancel a delivery (owner/manager). Keeps the row and reverses stock."""
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=MANAGE_ROLES,
    )
    reason = _clean_reason(reason)
    delivery = _lock_delivery(business_id, delivery_id)
    _cancel_locked(delivery, user=user, reason=reason)

    # A visit with nothing left delivered is unresolved again.
    visit = delivery.visit
    if visit is not None and not visit.deliveries.filter(
        status=Delivery.Status.DELIVERED,
    ).exists():
        visit.status = "PENDING"
        visit.reason = ""
        visit.save(update_fields=["status", "reason"])

    return delivery


@transaction.atomic
def correct_delivery(
    *,
    business_id,
    delivery_id,
    user,
    reason,
    quantity=None,
    shift=None,
    unit_price=None,
    notes=None,
):
    """
    Correct a delivery (owner/manager) by cancelling it and recording a
    replacement at the original timestamp, so the price in force at the
    time of delivery still applies. Returns the replacement.
    """
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=MANAGE_ROLES,
    )
    reason = _clean_reason(reason)
    old = _lock_delivery(business_id, delivery_id)

    if unit_price is not None and not old.product.allow_open_price:
        raise ValidationError({
            "unit_price": "The price of this product cannot be set manually."
        })

    new_quantity = old.quantity if quantity is None else Decimal(str(quantity))
    new_shift = old.shift if shift is None else shift
    new_notes = old.notes if notes is None else notes
    new_price = old.unit_price if unit_price is None else Decimal(str(unit_price))

    if (new_quantity, new_shift, new_price, new_notes) == (
        old.quantity, old.shift, old.unit_price, old.notes,
    ):
        raise ValidationError("Nothing to correct.")

    _cancel_locked(old, user=user, reason=f"Corrected: {reason}")

    return create_delivery(
        business_id=business_id,
        customer_id=old.customer_id,
        product_id=old.product_id,
        quantity=new_quantity,
        user=user,
        delivered_at=old.delivered_at,
        shift=new_shift,
        # Only open-price products take a price; others re-resolve it
        # at the original timestamp.
        unit_price=new_price if old.product.allow_open_price else None,
        notes=new_notes,
        replaces=old,
        visit=old.visit,
    )
