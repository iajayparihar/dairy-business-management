"""Customer balance and account statement (positive = the customer owes)."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db.models import Count, Exists, OuterRef, Sum
from django.utils import timezone

from apps.billing import selectors
from apps.billing.models import (
    CustomerBill,
    CustomerBillLine,
    CustomerPayment,
    CustomerPaymentReversal,
)
from apps.businesses.models import Business
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.sales.models import Sale

ZERO = selectors.ZERO


def _sum(queryset, field="amount"):
    return queryset.aggregate(total=Sum(field))["total"] or ZERO


def get_customer_balance(*, business_id, customer_id):
    """
    The position plus what is delivered but not yet on a finalized bill.
    unbilled_* is reported beside the balance and never added to it.
    """
    unbilled = Delivery.objects.filter(
        business_id=business_id,
        customer_id=customer_id,
        status=Delivery.Status.DELIVERED,
    ).exclude(
        Exists(
            CustomerBillLine.objects.filter(
                delivery=OuterRef("pk"),
                bill__status=CustomerBill.Status.FINALIZED,
            )
        )
    )
    totals = unbilled.aggregate(amount=Sum("line_total"), count=Count("pk"))

    # Shop credit sales not yet on a finalized bill, reported the same way.
    unbilled_sales = Sale.objects.filter(
        business_id=business_id,
        customer_id=customer_id,
        payment_mode=Sale.PaymentMode.CREDIT,
        status=Sale.Status.COMPLETED,
    ).exclude(
        Exists(
            CustomerBillLine.objects.filter(
                sale_line__sale=OuterRef("pk"),
                bill__status=CustomerBill.Status.FINALIZED,
            )
        )
    )
    sold = unbilled_sales.aggregate(amount=Sum("total_amount"), count=Count("pk"))

    return {
        **selectors.get_customer_position(
            business_id=business_id, customer_id=customer_id
        ),
        "unbilled_amount": (totals["amount"] or ZERO) + (sold["amount"] or ZERO),
        "unbilled_deliveries": totals["count"],
        "unbilled_sales": sold["count"],
    }


def _parse_day(value, name):
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValidationError({name: "Use YYYY-MM-DD format."})
    return value


def get_customer_account_statement(
    *, business_id, customer_id, start_date=None, end_date=None
):
    """
    Entries: BILL (when finalized), BILL_VOID (when voided), PAYMENT,
    PAYMENT_REVERSAL, with a running balance. Days are half-open
    [start 00:00, end + 1 day 00:00) in Business.timezone. Because a void is
    its own entry at its own time, a statement for an old period reads the
    same after a later void.
    closing_balance == outstanding_amount - available_credit == net_balance.
    """
    start_date = _parse_day(start_date, "start_date")
    end_date = _parse_day(end_date, "end_date")

    if start_date and end_date and start_date > end_date:
        raise ValidationError(
            {"end_date": "End date must be on or after start date."}
        )

    business = Business.objects.get(pk=business_id)
    customer = Customer.all_objects.get(pk=customer_id, business_id=business_id)
    tz = ZoneInfo(business.timezone)
    end_date = end_date or timezone.now().astimezone(tz).date()
    start_at = datetime.combine(start_date, time.min, tzinfo=tz) if start_date else None
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=tz)

    finalized = CustomerBill.objects.filter(
        business_id=business_id,
        customer_id=customer_id,
        finalized_at__isnull=False,
    )
    voided = finalized.filter(voided_at__isnull=False)
    payments = CustomerPayment.objects.filter(
        business_id=business_id, customer_id=customer_id
    )
    reversals = CustomerPaymentReversal.objects.filter(
        business_id=business_id, payment__customer_id=customer_id
    )

    if start_at:
        opening = (
            _sum(finalized.filter(finalized_at__lt=start_at), "total_amount")
            - _sum(voided.filter(voided_at__lt=start_at), "total_amount")
            - _sum(payments.filter(paid_at__lt=start_at))
            + _sum(reversals.filter(reversed_at__lt=start_at))
        )
    else:
        opening = ZERO

    def within(queryset, field):
        queryset = queryset.filter(**{f"{field}__lt": end_at})
        if start_at:
            queryset = queryset.filter(**{f"{field}__gte": start_at})
        return queryset

    events = []

    for bill in within(finalized, "finalized_at"):
        events.append({
            "timestamp": bill.finalized_at, "priority": 1, "type": "BILL",
            "reference": bill.bill_number,
            "description": (
                "Opening balance brought forward"
                if bill.bill_type == CustomerBill.BillType.OPENING
                else f"Bill {bill.period_start} to {bill.period_end}"
            ),
            "amount": bill.total_amount,
            "debit": bill.total_amount, "credit": ZERO,
        })

    for bill in within(voided, "voided_at"):
        events.append({
            "timestamp": bill.voided_at, "priority": 2, "type": "BILL_VOID",
            "reference": bill.bill_number,
            "description": f"Bill voided: {bill.void_reason}",
            "amount": bill.total_amount,
            "debit": ZERO, "credit": bill.total_amount,
        })

    for payment in within(payments, "paid_at"):
        events.append({
            "timestamp": payment.paid_at, "priority": 3, "type": "PAYMENT",
            "reference": payment.payment_number,
            "description": f"Payment ({payment.get_payment_method_display()})",
            "amount": payment.amount,
            "debit": ZERO, "credit": payment.amount,
        })

    for reversal in within(reversals, "reversed_at").select_related("payment"):
        events.append({
            "timestamp": reversal.reversed_at, "priority": 4,
            "type": "PAYMENT_REVERSAL",
            "reference": reversal.payment.payment_number,
            "description": f"Payment reversal: {reversal.reason}",
            "amount": reversal.amount,
            "debit": reversal.amount, "credit": ZERO,
        })

    events.sort(key=lambda e: (e["timestamp"], e["priority"], e["reference"]))

    running = opening
    for event in events:
        running += event["debit"] - event["credit"]
        event["balance"] = running

    return {
        "business_id": str(business_id),
        "business_name": business.name,
        "currency": business.currency,
        "timezone": business.timezone,
        "customer_id": str(customer_id),
        "customer_code": customer.customer_code,
        "customer_name": customer.name,
        "generated_at": timezone.now(),
        "start_date": start_date,
        "end_date": end_date,
        "opening_balance": opening,
        "transactions": events,
        "closing_balance": running,
        **selectors.get_customer_position(
            business_id=business_id, customer_id=customer_id, as_of=end_at
        ),
    }
