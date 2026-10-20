"""
Deliveries, recorded the way the app records them: through daily route runs.

Every past day and shift each route is started, every visit gets an outcome
(mostly DELIVERED from the standing order; a few ABSENT / NOT_DELIVERED;
SKIPPED when the customer has no order in that shift) and the run is closed.
Runs from the last 48 hours are worked by the assigned staff member, older
ones by the manager (staff cannot backdate).

Today's morning run is left OPEN with half the visits still PENDING, so a
staff login has real work to finish. Afterwards two closed runs are reopened
(with a reason) to cancel one delivery and correct another.
"""
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

from apps.deliveries.models import Delivery
from apps.deliveries.services import cancel_delivery, correct_delivery
from apps.routes.models import RouteVisit
from apps.routes.services import close_run, record_entry, reopen_run, start_run

from .base import at, history_days, is_past, recent, rng, this_month, today

SHIFT_HOUR = {"MORNING": 6, "EVENING": 17}
NOT_DELIVERED = ["Door locked, no answer.", "Customer asked to skip today.", "Gate closed, guard not there."]


def _outcome(t, customer_id, shift):
    orders = t.orders.get(customer_id, {}).get(shift)
    if not orders:
        return "SKIPPED", [], "No order in this shift."
    roll = rng.random()
    if roll < 0.03:
        return "ABSENT", [], ""
    if roll < 0.05:
        return "NOT_DELIVERED", [], rng.choice(NOT_DELIVERED)
    items = []
    for product, qty in orders:
        qty = Decimal(qty)
        if product.unit == "L" and rng.random() < 0.08:
            qty += Decimal("0.5")  # guests at home: a little extra
        items.append({"product_id": product.pk, "quantity": qty})
    return "DELIVERED", items, ""


def seed(t):
    biz = t.business
    for day in history_days():
        for shift, hour in SHIFT_HOUR.items():
            is_today = day == today()
            if not is_past(at(day, hour)) or (is_today and shift == "EVENING"):
                continue
            for route in t.routes:
                run = start_run(
                    business_id=biz.pk, route_id=route.pk, user=t.manager,
                    run_date=day, shift=shift,
                )
                user = route.assigned_to.user if recent(day) else t.manager
                visits = list(run.visits.order_by("sequence"))
                if is_today:
                    visits = visits[: len(visits) // 2]
                for visit in visits:
                    outcome, items, reason = _outcome(t, visit.customer_id, shift)
                    record_entry(
                        business_id=biz.pk, run_id=run.pk, customer_id=visit.customer_id,
                        user=user, outcome=outcome, items=items, reason=reason,
                    )
                if not is_today:
                    close_run(business_id=biz.pk, run_id=run.pk, user=user)

    # Audit trail examples, inside this month (last month gets billed).
    month = Delivery.objects.filter(
        business=biz, status="DELIVERED", product__unit="L",
        delivered_at__gte=at(max(this_month(), today() - timedelta(days=6)), 0),
        delivered_at__lt=at(today(), 0),
    ).order_by("delivered_at", "pk")[:2]
    if len(month) < 2:
        return
    first, second = month
    cancel_route_delivery(t, first, "Customer returned the milk: it was not ordered.")
    with _reopened(t, second, "Delivery boy noted 1 L less than given."):
        correct_delivery(
            business_id=biz.pk, delivery_id=second.pk, user=t.manager,
            reason="Delivery boy noted 1 L less than given.",
            quantity=second.quantity + Decimal("1"),
        )


def cancel_route_delivery(t, delivery, reason):
    """Reopen the run, cancel, give the visit a new outcome if it emptied, close."""
    with _reopened(t, delivery, reason):
        cancel_delivery(
            business_id=t.business.pk, delivery_id=delivery.pk, user=t.manager, reason=reason,
        )
        visit = RouteVisit.objects.get(pk=delivery.visit_id)
        if visit.status == "PENDING":
            record_entry(
                business_id=t.business.pk, run_id=visit.run_id, customer_id=visit.customer_id,
                user=t.manager, outcome="NOT_DELIVERED", reason=reason,
            )


@contextmanager
def _reopened(t, delivery, reason):
    """A closed run must be reopened (with a reason) before its deliveries change."""
    run_id = delivery.visit.run_id
    reopen_run(business_id=t.business.pk, run_id=run_id, user=t.manager, reason=reason)
    yield
    close_run(business_id=t.business.pk, run_id=run_id, user=t.manager)
