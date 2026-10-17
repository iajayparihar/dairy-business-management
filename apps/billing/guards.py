"""
Rules other apps call so billing's promises hold.

A FINALIZED period bill closes that customer's period to deliveries until it
is voided; a customer with any history cannot be deleted.
"""
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError

from apps.billing.models import CustomerBill, CustomerPayment
from apps.deliveries.models import Delivery
from apps.sales.models import Sale


def find_billed_period(business_id, customer_id, local_date):
    """The FINALIZED period bill of the customer that covers this local day."""
    return CustomerBill.objects.filter(
        business_id=business_id,
        customer_id=customer_id,
        bill_type=CustomerBill.BillType.PERIOD,
        status=CustomerBill.Status.FINALIZED,
        period_start__lte=local_date,
        period_end__gte=local_date,
    ).first()


def assert_period_open(business, customer_id, at):
    """
    Refuse to create, cancel or correct a delivery dated inside a finalized
    bill. `at` is the delivery's timestamp; the day is read in
    Business.timezone, like the bill's own period. Callers hold the customer
    lock, so a bill cannot be finalized between this check and the write.
    """
    local_date = at.astimezone(ZoneInfo(business.timezone)).date()
    bill = find_billed_period(business.pk, customer_id, local_date)

    if bill is not None:
        raise ValidationError(
            f"{local_date.isoformat()} falls in finalized bill "
            f"{bill.bill_number} ({bill.period_start.isoformat()} to "
            f"{bill.period_end.isoformat()}). Void the bill first, then "
            "change the delivery and issue the replacement."
        )


def customer_has_records(customer):
    """True when deleting the customer would hide deliveries, sales, bills or payments."""
    return (
        Delivery.objects.filter(
            customer=customer, status=Delivery.Status.DELIVERED
        ).exists()
        or Sale.objects.filter(
            customer=customer, status=Sale.Status.COMPLETED
        ).exists()
        or CustomerBill.objects.filter(customer=customer).exists()
        or CustomerPayment.objects.filter(customer=customer).exists()
    )
