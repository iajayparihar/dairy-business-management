import hashlib
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.businesses.models import BusinessMembership
from apps.businesses.services import require_business_membership
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.deliveries.services import MANAGE_ROLES, create_delivery
from apps.products.models import Product
from apps.routes.models import (
    Route,
    RouteCustomer,
    RouteRun,
    RouteVisit,
    StandingOrder,
)

# Default clock time for entering a past run's deliveries.
SHIFT_START = {"MORNING": time(6, 0), "EVENING": time(17, 0)}
NON_DELIVERY = {
    RouteVisit.Status.NOT_DELIVERED,
    RouteVisit.Status.ABSENT,
    RouteVisit.Status.SKIPPED,
}
# Fallback suggestion looks back this far for a customer's last delivery.
SUGGESTION_LOOKBACK = timedelta(days=14)


def _member(user, business_id, roles=None):
    return require_business_membership(
        user=user, business_id=business_id, allowed_roles=roles,
    )


def _assignee(business_id, membership_id):
    if membership_id is None:
        return None
    try:
        return BusinessMembership.objects.get(
            pk=membership_id, business_id=business_id, is_active=True,
        )
    except BusinessMembership.DoesNotExist as exc:
        raise ValidationError({
            "assigned_to": "Not an active member of this business."
        }) from exc


def can_work_run(membership, run):
    """Owners/managers work any run; staff only runs assigned to them."""
    return (
        membership.role in MANAGE_ROLES
        or run.assigned_to_id == membership.pk
    )


def _get_run(business_id, run_id, *, lock=False):
    qs = RouteRun.objects.select_related("route")
    if lock:
        qs = qs.select_for_update(of=("self",))
    return qs.get(pk=run_id, business_id=business_id)


def visible_runs(membership):
    qs = RouteRun.objects.filter(business_id=membership.business_id)
    if membership.role not in MANAGE_ROLES:
        qs = qs.filter(assigned_to=membership)
    return qs.select_related("route")


# ----------------------------------------------------------------- routes

@transaction.atomic
def create_route(*, business_id, user, name, assigned_to=None):
    _member(user, business_id, MANAGE_ROLES)
    name = (name or "").strip()
    if not name:
        raise ValidationError({"name": "A route name is required."})
    if Route.objects.filter(business_id=business_id, name__iexact=name).exists():
        raise ValidationError({"name": "A route with this name already exists."})

    return Route.objects.create(
        business_id=business_id,
        name=name,
        assigned_to=_assignee(business_id, assigned_to),
    )


@transaction.atomic
def set_route_customers(*, business_id, route_id, user, customer_ids):
    """Replace the ordered customer list of a route."""
    _member(user, business_id, MANAGE_ROLES)
    route = Route.objects.select_for_update().get(
        pk=route_id, business_id=business_id,
    )

    ids = list(customer_ids)
    if len(set(ids)) != len(ids):
        raise ValidationError({"customers": "A customer is listed twice."})

    customers = Customer.objects.filter(
        business_id=business_id, pk__in=ids, is_active=True,
    )
    if customers.count() != len(ids):
        raise ValidationError({
            "customers": "Unknown, inactive or foreign customer in the list."
        })

    elsewhere = RouteCustomer.objects.filter(
        customer_id__in=ids,
    ).exclude(route=route).select_related("route", "customer")
    if elsewhere.exists():
        stop = elsewhere.first()
        raise ValidationError({
            "customers": (
                f"{stop.customer.name} is already on route "
                f"'{stop.route.name}'."
            )
        })

    route.stops.all().delete()
    RouteCustomer.objects.bulk_create([
        RouteCustomer(route=route, business_id=business_id, customer_id=cid, sequence=i)
        for i, cid in enumerate(ids, start=1)
    ])

    return route


# ---------------------------------------------------------- standing orders

@transaction.atomic
def set_standing_order(*, business_id, user, customer_id, product_id, quantity, shift="MORNING"):
    _member(user, business_id, MANAGE_ROLES)
    try:
        customer = Customer.objects.get(pk=customer_id, business_id=business_id, is_active=True)
        product = Product.objects.get(pk=product_id, business_id=business_id, is_active=True)
    except (Customer.DoesNotExist, Product.DoesNotExist) as exc:
        raise ValidationError("Unknown customer or product for this business.") from exc

    from apps.deliveries.services import _validate_quantity
    quantity = _validate_quantity(quantity, product)

    order, _ = StandingOrder.objects.update_or_create(
        customer=customer, product=product, shift=shift,
        defaults={"business_id": business_id, "quantity": quantity, "is_active": True},
    )
    return order


@transaction.atomic
def remove_standing_order(*, business_id, user, order_id):
    _member(user, business_id, MANAGE_ROLES)
    order = StandingOrder.objects.select_for_update().get(
        pk=order_id, business_id=business_id,
    )
    order.is_active = False
    order.save(update_fields=["is_active", "updated_at"])
    return order


# -------------------------------------------------------------------- runs

@transaction.atomic
def start_run(*, business_id, route_id, user, run_date=None, shift="MORNING"):
    """
    Open the route for a date and shift, with one PENDING visit per
    customer in route order. Starting an existing run returns it.
    """
    _member(user, business_id, MANAGE_ROLES)
    run_date = run_date or timezone.localdate()

    if run_date > timezone.localdate():
        raise ValidationError({"run_date": "A route cannot be started for a future date."})
    if shift not in ("MORNING", "EVENING"):
        raise ValidationError({"shift": "Invalid shift."})

    route = Route.objects.select_for_update().get(
        pk=route_id, business_id=business_id, is_active=True,
    )
    run, created = RouteRun.objects.get_or_create(
        route=route, run_date=run_date, shift=shift,
        defaults={
            "business_id": business_id,
            "assigned_to": route.assigned_to,
            "created_by": user,
        },
    )

    if created:
        RouteVisit.objects.bulk_create([
            RouteVisit(run=run, business_id=business_id, customer_id=s.customer_id, sequence=s.sequence)
            for s in route.stops.filter(customer__is_active=True, customer__is_deleted=False)
        ])

    return run


def run_tasks(run):
    """
    Visits in route order with a suggested basket for each: the standing
    order for the shift, else the customer's last delivery of that shift
    (quantities only; prices are resolved when delivered).
    """
    visits = list(
        run.visits.select_related("customer").order_by("sequence")
    )
    customer_ids = [v.customer_id for v in visits]

    standing = {}
    for o in StandingOrder.objects.filter(
        customer_id__in=customer_ids, shift=run.shift, is_active=True,
    ).select_related("product"):
        standing.setdefault(o.customer_id, []).append(o)

    fallback = {}
    missing = [c for c in customer_ids if c not in standing]
    if missing:
        since = run.run_date - SUGGESTION_LOOKBACK
        latest = {}
        for d in Delivery.objects.filter(
            business_id=run.business_id,
            customer_id__in=missing,
            shift=run.shift,
            status=Delivery.Status.DELIVERED,
            delivery_date__lt=run.run_date,
            delivery_date__gte=since,
        ).select_related("product").order_by("-delivery_date"):
            day = latest.setdefault(d.customer_id, d.delivery_date)
            if d.delivery_date == day:
                fallback.setdefault(d.customer_id, []).append(d)

    tasks = []
    for v in visits:
        if v.customer_id in standing:
            source, items = "standing_order", [(o.product, o.quantity) for o in standing[v.customer_id]]
        elif v.customer_id in fallback:
            source, items = "last_delivery", [(d.product, d.quantity) for d in fallback[v.customer_id]]
        else:
            source, items = None, []
        tasks.append({
            "visit": v,
            "suggestion_source": source,
            "suggestion": [
                {"product_id": p.pk, "product_name": p.name, "unit": p.unit, "quantity": q}
                for p, q in items
            ],
        })

    return tasks


def _delivered_at(run):
    if run.run_date == timezone.localdate():
        return timezone.now()
    return datetime.combine(run.run_date, SHIFT_START[run.shift], tzinfo=timezone.get_current_timezone())


def _item_ref(client_ref, product_id):
    digest = hashlib.sha1(f"{client_ref}:{product_id}".encode()).hexdigest()
    return f"visit-{digest}"


@transaction.atomic
def record_entry(
    *, business_id, run_id, customer_id, user, outcome,
    items=(), reason="", client_ref=None,
):
    """
    Record the outcome of one customer visit. All of it commits or none.

    DELIVERED needs at least one item and creates one Delivery per item
    (prices from the server). Other outcomes record why nothing was
    delivered. Re-sending with the same client_ref is safe.
    """
    membership = _member(user, business_id)
    run = _get_run(business_id, run_id, lock=True)

    if not can_work_run(membership, run):
        raise PermissionDenied("This route run is not assigned to you.")
    if run.status != RouteRun.Status.OPEN:
        raise ValidationError("This route run is closed.")

    visit = RouteVisit.objects.select_for_update().get(
        run=run, customer_id=customer_id,
    )
    reason = (reason or "").strip()

    if outcome == RouteVisit.Status.DELIVERED:
        if not items:
            raise ValidationError({"items": "Add at least one item to deliver."})
        for item in items:
            create_delivery(
                business_id=business_id,
                customer_id=customer_id,
                product_id=item["product_id"],
                quantity=item["quantity"],
                unit_price=item.get("unit_price"),
                user=user,
                delivered_at=_delivered_at(run),
                shift=run.shift,
                client_ref=_item_ref(client_ref, item["product_id"]) if client_ref else None,
                notes=item.get("notes", ""),
                visit=visit,
            )
    elif outcome in NON_DELIVERY:
        if items:
            raise ValidationError({"items": "Items are only allowed for a delivered visit."})
        if visit.status == RouteVisit.Status.DELIVERED:
            raise ValidationError(
                "This visit already has deliveries. Cancel them first."
            )
        if outcome != RouteVisit.Status.ABSENT and not reason:
            raise ValidationError({"reason": "A reason is required."})
    else:
        raise ValidationError({"outcome": "Invalid outcome."})

    visit.status = outcome
    visit.reason = reason if outcome in NON_DELIVERY else ""
    visit.recorded_by = user
    visit.recorded_at = timezone.now()
    visit.save(update_fields=["status", "reason", "recorded_by", "recorded_at"])

    return visit


def run_summary(run):
    counts = {s: 0 for s in RouteVisit.Status.values}
    for row in run.visits.values("status").annotate(n=Count("pk")):
        counts[row["status"]] = row["n"]

    deliveries = Delivery.objects.filter(
        visit__run=run, status=Delivery.Status.DELIVERED,
    )
    products = list(
        deliveries.values("product_id", "product__name", "product__unit")
        .annotate(quantity=Sum("quantity"), amount=Sum("line_total"))
        .order_by("product__name")
    )

    return {
        "visits": counts,
        "total_visits": sum(counts.values()),
        "products": products,
        "total_amount": sum((p["amount"] for p in products), Decimal("0.00")),
        "stock_shortfalls": deliveries.filter(stock_shortfall__gt=0).count(),
    }


@transaction.atomic
def close_run(*, business_id, run_id, user):
    membership = _member(user, business_id)
    run = _get_run(business_id, run_id, lock=True)

    if not can_work_run(membership, run):
        raise PermissionDenied("This route run is not assigned to you.")
    if run.status == RouteRun.Status.CLOSED:
        raise ValidationError("This route run is already closed.")

    pending = run.visits.filter(status=RouteVisit.Status.PENDING).count()
    if pending:
        raise ValidationError(
            f"{pending} customer visit(s) still have no outcome. "
            "Record each as delivered, absent, skipped or not delivered."
        )

    run.status = RouteRun.Status.CLOSED
    run.closed_at = timezone.now()
    run.closed_by = user
    run.save(update_fields=["status", "closed_at", "closed_by"])

    return run


@transaction.atomic
def reopen_run(*, business_id, run_id, user, reason):
    _member(user, business_id, MANAGE_ROLES)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})

    run = _get_run(business_id, run_id, lock=True)
    if run.status != RouteRun.Status.CLOSED:
        raise ValidationError("Only a closed route run can be reopened.")

    run.status = RouteRun.Status.OPEN
    run.reopened_at = timezone.now()
    run.reopened_by = user
    run.reopen_reason = reason
    run.save(update_fields=["status", "reopened_at", "reopened_by", "reopen_reason"])

    return run
