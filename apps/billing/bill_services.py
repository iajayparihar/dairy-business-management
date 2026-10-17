"""
Bill lifecycle: generate a draft, finalize, void, record an opening balance.

DRAFT -> FINALIZED -> VOID (or DRAFT -> VOID). A draft stores no lines: its
numbers are computed live from the deliveries, so it can never go stale. At
finalize the lines are copied onto the bill and the bill is frozen: its
period is then closed to deliveries (apps/deliveries/services) until the bill
is voided. Every writer takes the customer lock first (apps/billing/locks).
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing import selectors
from apps.billing.locks import lock_customer
from apps.billing.models import CustomerBill, CustomerBillLine, CustomerPayment
from apps.billing.money import to_money
from apps.billing.payment_services import (
    apply_credit_to_bill,
    record_customer_payment,
)
from apps.businesses.models import Business
from apps.businesses.services import require_business_membership
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.inventory.models import BusinessMilkConfig
from apps.purchases.milk_queries import local_day_bounds

ALLOWED_ROLES = ["OWNER", "MANAGER"]
MAX_PERIOD_DAYS = 366
MIN_REASON_LENGTH = 3
ZERO = selectors.ZERO
Status = CustomerBill.Status
BillType = CustomerBill.BillType
Kind = CustomerBillLine.Kind


class BillConflict(ValidationError):
    """The request conflicts with the bills as they are (API answers 409)."""


class AlreadyBilled(BillConflict):
    reason = "ALREADY_BILLED"


class OverlapsBilled(BillConflict):
    reason = "OVERLAPS_BILLED"


class NothingToBill(ValidationError):
    reason = "NO_DELIVERIES"


class CustomerNotFound(ValidationError):
    reason = "CUSTOMER_NOT_FOUND"


def _check_period(period_start, period_end):
    for name, value in (("period_start", period_start), ("period_end", period_end)):
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ValidationError({name: "Provide a date (YYYY-MM-DD)."})

    if period_end < period_start:
        raise ValidationError({
            "period_end": "The period must end on or after its start date."
        })

    if (period_end - period_start).days + 1 > MAX_PERIOD_DAYS:
        raise ValidationError({
            "period_end": f"A bill can cover at most {MAX_PERIOD_DAYS} days."
        })


def period_bounds(business, period_start, period_end):
    """Half-open instants [first day 00:00, day after last 00:00) in business time."""
    return (
        local_day_bounds(business, period_start)[0],
        local_day_bounds(business, period_end)[1],
    )


def billable_deliveries(business, customer_id, period_start, period_end):
    """
    DELIVERED, undeleted deliveries of the customer in the period. Filters on
    delivered_at in Business.timezone, never on Delivery.delivery_date (that
    one follows the server timezone).
    """
    start_at, end_at = period_bounds(business, period_start, period_end)
    return Delivery.objects.filter(
        business_id=business.pk,
        customer_id=customer_id,
        status=Delivery.Status.DELIVERED,
        delivered_at__gte=start_at,
        delivered_at__lt=end_at,
    )


def _milk_kinds(business_id):
    """Product id -> milk kind from the milk mapping, plus any warnings."""
    config = BusinessMilkConfig.objects.filter(business_id=business_id).first()
    kinds = {}

    if config is not None:
        if config.cow_product_id:
            kinds[config.cow_product_id] = Kind.COW_MILK
        if config.buffalo_product_id:
            kinds[config.buffalo_product_id] = Kind.BUFFALO_MILK

    warnings = []
    if config is None or not (config.cow_product_id and config.buffalo_product_id):
        warnings.append({
            "code": "MILK_CONFIG_INCOMPLETE",
            "message": (
                "The cow / buffalo milk mapping is not fully set, so milk "
                "that is not mapped is listed under other items. Set it in "
                "milk-config."
            ),
        })

    return kinds, warnings


def build_bill_lines(business, customer_id, period_start, period_end):
    """
    The lines a bill for this customer and period would carry, as dicts:
    one per delivery, in delivery order. amount is the delivery's stored
    line_total (never recomputed). Returns (rows, warnings).
    """
    kinds, warnings = _milk_kinds(business.pk)
    deliveries = (
        billable_deliveries(business, customer_id, period_start, period_end)
        .select_related("product")
        .order_by("delivered_at", "pk")
    )
    rows = [
        {
            "delivery_id": delivery.pk,
            "product_id": delivery.product_id,
            "line_kind": kinds.get(delivery.product_id, Kind.OTHER),
            "description": delivery.product.name,
            "unit": delivery.product.unit,
            "delivered_at": delivery.delivered_at,
            "shift": delivery.shift,
            "quantity": delivery.quantity,
            "unit_price": delivery.unit_price,
            "amount": delivery.line_total,
        }
        for delivery in deliveries
    ]
    return rows, warnings


def _number_prefix(customer, day, opening=False):
    return "{}-{}{:%y%m%d}-".format(
        customer.customer_code, "OB-" if opening else "", day
    )


def next_bill_number(customer, day, opening=False):
    """
    <CUSTOMER_CODE>-<YYMMDD of the period start>-<seq>; an opening balance is
    <CUSTOMER_CODE>-OB-<YYMMDD>-<seq>. Counts every bill ever written for the
    prefix (voided and discarded drafts included), so a number is never
    reused and a replacement gets the next one. Callers hold the customer
    lock; uniq_customer_bill_number is the final guard.
    """
    prefix = _number_prefix(customer, day, opening)
    used = [
        int(number[len(prefix):])
        for number in CustomerBill.objects.filter(
            business_id=customer.business_id,
            customer=customer,
            bill_number__startswith=prefix,
        ).values_list("bill_number", flat=True)
        if number[len(prefix):].isdigit()
    ]
    return f"{prefix}{max(used, default=0) + 1}"


def _locked_customer(business_id, customer_id):
    customer = lock_customer(business_id, customer_id)

    if customer is None:
        raise CustomerNotFound({
            "customer": (
                "The customer does not exist or belongs to another business."
            )
        })

    return customer


def _peek_bill(bill_id, business_id):
    """Tenant-scoped, unlocked read of a bill's customer (no leak)."""
    try:
        return CustomerBill.objects.only("customer_id").get(
            pk=bill_id, business_id=business_id
        )
    except (CustomerBill.DoesNotExist, ValidationError, ValueError):
        raise ValidationError("Bill not found.")


def _overlapping_final_bill(business_id, customer_id, start, end, exclude_pk=None):
    clash = CustomerBill.objects.filter(
        business_id=business_id,
        customer_id=customer_id,
        bill_type=BillType.PERIOD,
        status=Status.FINALIZED,
        period_start__lte=end,
        period_end__gte=start,
    )

    if exclude_pk is not None:
        clash = clash.exclude(pk=exclude_pk)

    return clash.order_by("period_start").first()


def _overlap_message(bill):
    return (
        f"The period overlaps finalized bill {bill.bill_number} "
        f"({bill.period_start} to {bill.period_end}). Void that bill first."
    )


@transaction.atomic
def generate_draft_bill(
    *, business_id, customer_id, period_start, period_end, user
):
    """
    Create the DRAFT bill for a customer and period, or return the existing
    one (`.created` is False). Refuses a period that is already billed, or
    overlaps a finalized bill, or has no deliveries. Inactive customers can
    be billed.
    """
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=ALLOWED_ROLES,
    )
    _check_period(period_start, period_end)

    customer = _locked_customer(business_id, customer_id)
    business = Business.objects.get(pk=business_id)

    live = CustomerBill.objects.filter(
        business_id=business_id,
        customer=customer,
        bill_type=BillType.PERIOD,
        period_start=period_start,
        period_end=period_end,
        status__in=[Status.DRAFT, Status.FINALIZED],
    ).first()

    if live is not None:
        if live.status == Status.DRAFT:
            live.created = False
            return live
        raise AlreadyBilled({
            "detail": (
                f"This period is already billed: {live.bill_number}. "
                "Void it to issue a replacement."
            ),
            "bill_id": str(live.pk),
        })

    clash = _overlapping_final_bill(
        business_id, customer.pk, period_start, period_end
    )
    if clash is not None:
        raise OverlapsBilled({
            "detail": _overlap_message(clash), "bill_id": str(clash.pk),
        })

    if not billable_deliveries(
        business, customer.pk, period_start, period_end
    ).exists():
        raise NothingToBill(
            "Nothing to bill: the customer has no deliveries in this period."
        )

    bill = CustomerBill.objects.create(
        business_id=business_id,
        customer=customer,
        bill_type=BillType.PERIOD,
        bill_number=next_bill_number(customer, period_start),
        period_start=period_start,
        period_end=period_end,
        status=Status.DRAFT,
        created_by=user,
    )
    bill.created = True
    return bill


def generate_drafts(
    *, business_id, period_start, period_end, user, customer_ids=None
):
    """
    Draft bills for many customers. Each customer is its own transaction, so
    one problem never blocks the rest. Without customer_ids: every customer
    with deliveries in the period. Returns
    {"created": [...], "existing": [...], "skipped": [{customer_id, reason}]}.
    """
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=ALLOWED_ROLES,
    )
    _check_period(period_start, period_end)

    if customer_ids is None:
        business = Business.objects.get(pk=business_id)
        wanted = set(
            billable_deliveries_in_business(
                business, period_start, period_end
            )
        )
    else:
        try:
            wanted = {UUID(str(cid)) for cid in customer_ids}
        except (TypeError, ValueError):
            raise ValidationError({
                "customer_ids": "Each customer id must be a valid UUID."
            })

    # Stable, readable order: by customer name. Ids that match no customer
    # of this business come last and are reported as skipped.
    ordered = list(
        Customer.all_objects.filter(business_id=business_id, pk__in=wanted)
        .order_by("name", "pk")
        .values_list("pk", flat=True)
    )
    known = set(ordered)
    unknown = sorted(cid for cid in wanted if cid not in known)

    result = {"created": [], "existing": [], "skipped": []}

    for customer_id in ordered + unknown:
        try:
            bill = generate_draft_bill(
                business_id=business_id,
                customer_id=customer_id,
                period_start=period_start,
                period_end=period_end,
                user=user,
            )
        except (AlreadyBilled, OverlapsBilled, NothingToBill, CustomerNotFound) as exc:
            result["skipped"].append({
                "customer_id": customer_id, "reason": exc.reason,
            })
        else:
            result["created" if bill.created else "existing"].append(bill)

    return result


def billable_deliveries_in_business(business, period_start, period_end):
    """Ids of the customers that have billable deliveries in the period."""
    start_at, end_at = period_bounds(business, period_start, period_end)
    return (
        Delivery.objects.filter(
            business_id=business.pk,
            status=Delivery.Status.DELIVERED,
            delivered_at__gte=start_at,
            delivered_at__lt=end_at,
        )
        .values_list("customer_id", flat=True)
        .distinct()
    )


def _header_snapshot(business, customer):
    return {
        "business": {
            "name": business.name,
            "address": business.address,
            "phone": business.phone,
            "email": business.email,
            "currency": business.currency,
            "timezone": business.timezone,
        },
        "customer": {
            "code": customer.customer_code,
            "name": customer.name,
            "phone": customer.phone,
            "address": customer.address,
        },
    }


@transaction.atomic
def finalize_bill(*, business_id, bill_id, user, expected_total=None):
    """
    Freeze a DRAFT bill: copy its lines, stamp it, and apply the customer's
    held advance to it. Returns the bill; `.replayed` is True when it was
    already finalized, `.credit_applied` lists the allocations made now.

    expected_total: what the caller reviewed. If the live total differs the
    bill is left untouched and BillConflict is raised.
    Lock order: customer -> bill -> payments.
    """
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=ALLOWED_ROLES,
    )

    if expected_total is not None:
        expected_total = to_money(expected_total, "expected_total", allow_zero=True)

    ref = _peek_bill(bill_id, business_id)
    customer = _locked_customer(business_id, ref.customer_id)
    bill = CustomerBill.objects.select_for_update().get(
        pk=bill_id, business_id=business_id
    )

    if bill.status == Status.FINALIZED:
        if expected_total is not None and bill.total_amount != expected_total:
            raise BillConflict({
                "expected_total": str(expected_total),
                "current_total": str(bill.total_amount),
            })
        bill.replayed = True
        bill.credit_applied = []
        return bill

    if bill.status == Status.VOID:
        raise ValidationError("A void bill cannot be finalized.")

    business = Business.objects.get(pk=business_id)
    today = timezone.now().astimezone(ZoneInfo(business.timezone)).date()

    if bill.period_end >= today:
        raise ValidationError({
            "period_end": (
                "The period has not ended yet. Finalizing would close it to "
                "new deliveries; finalize from "
                f"{(bill.period_end + timedelta(days=1)).isoformat()}."
            )
        })

    rows, warnings = build_bill_lines(
        business, customer.pk, bill.period_start, bill.period_end
    )

    if not rows:
        raise NothingToBill(
            "Nothing to bill: the customer has no deliveries in this period."
        )

    total = sum((row["amount"] for row in rows), ZERO)

    if expected_total is not None and total != expected_total:
        raise BillConflict({
            "expected_total": str(expected_total),
            "current_total": str(total),
        })

    clash = _overlapping_final_bill(
        business_id, customer.pk, bill.period_start, bill.period_end,
        exclude_pk=bill.pk,
    )
    if clash is not None:
        raise OverlapsBilled({
            "detail": _overlap_message(clash), "bill_id": str(clash.pk),
        })

    # Customer money before this bill (it is still a draft, so not counted).
    balance_before = selectors.get_customer_position(
        business_id=business_id, customer_id=customer.pk
    )["net_balance"]

    bill.status = Status.FINALIZED
    bill.total_amount = total
    bill.balance_before = balance_before
    bill.header_snapshot = _header_snapshot(business, customer)
    bill.finalized_at = timezone.now()
    bill.finalized_by = user

    try:
        # Savepoint: the exclusion constraint is the final guard against
        # billing a delivery twice.
        with transaction.atomic():
            bill.save(update_fields=[
                "status", "total_amount", "balance_before", "header_snapshot",
                "finalized_at", "finalized_by", "updated_at",
            ])
    except IntegrityError:
        raise OverlapsBilled(
            "Another finalized bill overlaps this period. Reload and retry."
        )

    CustomerBillLine.objects.bulk_create([
        CustomerBillLine(business_id=business_id, bill=bill, **row)
        for row in rows
    ])

    bill.credit_applied = apply_credit_to_bill(bill=bill)
    bill.warnings = warnings
    bill.replayed = False
    return bill


@transaction.atomic
def void_bill(
    *, business_id, bill_id, user, reason, create_replacement=None
):
    """
    Void a DRAFT (discard) or FINALIZED bill. Returns (bill, replacement).

    A voided bill stays on record. Payments allocated to it become the
    customer's advance again. For a FINALIZED period bill a replacement DRAFT
    for the same period is created unless create_replacement is False; asking
    for one on a draft or an opening balance is an error.
    """
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=ALLOWED_ROLES,
    )

    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_LENGTH:
        raise ValidationError({
            "reason": (
                f"A reason of at least {MIN_REASON_LENGTH} characters is "
                "required to void a bill."
            )
        })

    ref = _peek_bill(bill_id, business_id)
    customer = _locked_customer(business_id, ref.customer_id)
    bill = CustomerBill.objects.select_for_update().get(
        pk=bill_id, business_id=business_id
    )

    if bill.status == Status.VOID:
        raise ValidationError("This bill is already void.")

    replaceable = (
        bill.status == Status.FINALIZED and bill.bill_type == BillType.PERIOD
    )

    if create_replacement is None:
        create_replacement = replaceable
    elif create_replacement and not replaceable:
        raise ValidationError({
            "create_replacement": (
                "Only a finalized period bill can have a replacement."
            )
        })

    bill.status = Status.VOID
    bill.voided_at = timezone.now()
    bill.voided_by = user
    bill.void_reason = reason
    bill.save(update_fields=sorted(CustomerBill.VOID_FIELDS))

    replacement = None

    if create_replacement:
        replacement = CustomerBill.objects.create(
            business_id=business_id,
            customer=customer,
            bill_type=BillType.PERIOD,
            bill_number=next_bill_number(customer, bill.period_start),
            period_start=bill.period_start,
            period_end=bill.period_end,
            status=Status.DRAFT,
            replaces=bill,
            created_by=user,
        )

    return bill, replacement


@transaction.atomic
def record_opening_balance(
    *, business_id, customer_id, amount, as_of_date, user, client_ref=None
):
    """
    Enter what a customer owed (amount > 0) or had prepaid (amount < 0) in the
    paper register. An owed amount becomes a FINALIZED OPENING bill (one live
    per customer; void it to enter a different one). A prepaid amount becomes
    an advance payment. Returns SimpleNamespace(kind, bill, payment, replayed).
    """
    require_business_membership(
        user=user, business_id=business_id, allowed_roles=ALLOWED_ROLES,
    )

    amount = to_money(amount, "amount", signed=True)

    if not isinstance(as_of_date, date) or isinstance(as_of_date, datetime):
        raise ValidationError({"as_of_date": "Provide a date (YYYY-MM-DD)."})

    customer = _locked_customer(business_id, customer_id)
    business = Business.objects.get(pk=business_id)
    today = timezone.now().astimezone(ZoneInfo(business.timezone)).date()

    if as_of_date > today:
        raise ValidationError({
            "as_of_date": "The balance date cannot be in the future."
        })

    if amount < 0:
        payment = record_customer_payment(
            business_id=business_id,
            customer_id=customer.pk,
            amount=-amount,
            user=user,
            payment_method=CustomerPayment.PaymentMethod.OTHER,
            notes=f"Opening advance (balance brought forward as of {as_of_date}).",
            client_ref=client_ref,
        )
        return SimpleNamespace(
            kind="ADVANCE", bill=None, payment=payment,
            replayed=payment.replayed,
        )

    existing = CustomerBill.objects.filter(
        business_id=business_id,
        customer=customer,
        bill_type=BillType.OPENING,
        status=Status.FINALIZED,
    ).first()

    if existing is not None:
        if existing.total_amount == amount and existing.period_start == as_of_date:
            return SimpleNamespace(
                kind="BILL", bill=existing, payment=None, replayed=True,
            )
        raise BillConflict({
            "detail": (
                f"An opening balance already exists ({existing.bill_number}). "
                "Void it first to enter a different one."
            ),
            "bill_id": str(existing.pk),
        })

    balance_before = selectors.get_customer_position(
        business_id=business_id, customer_id=customer.pk
    )["net_balance"]

    bill = CustomerBill.objects.create(
        business_id=business_id,
        customer=customer,
        bill_type=BillType.OPENING,
        bill_number=next_bill_number(customer, as_of_date, opening=True),
        period_start=as_of_date,
        period_end=as_of_date,
        status=Status.FINALIZED,
        total_amount=amount,
        balance_before=balance_before,
        header_snapshot=_header_snapshot(business, customer),
        finalized_at=timezone.now(),
        finalized_by=user,
        created_by=user,
    )
    bill.credit_applied = apply_credit_to_bill(bill=bill)
    return SimpleNamespace(kind="BILL", bill=bill, payment=None, replayed=False)
