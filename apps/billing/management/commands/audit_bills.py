from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from apps.billing import selectors
from apps.billing.bill_services import billable_deliveries, billable_sale_lines
from apps.billing.models import (
    CustomerBill,
    CustomerBillLine,
    CustomerPayment,
    CustomerPaymentAllocation,
)
from apps.businesses.models import Business

ZERO = selectors.ZERO


class Command(BaseCommand):
    """
    Read-only audit of customer money. Run after deploys and migrations, or
    when a balance looks wrong. Exit status 1 means a promise is broken.

    Checks, per FINALIZED bill: total equals its lines; every line still
    matches its delivery; the bill covers exactly the customer's DELIVERED
    deliveries in its period; periods of one customer do not overlap. Per
    payment and bill: allocations never exceed what is there to apply. Per
    customer: net balance equals outstanding minus advance.
    """

    help = "Report broken invariants in bills, allocations and balances."

    def handle(self, *args, **options):
        problems = []
        bills = CustomerBill.objects.filter(status="FINALIZED").select_related(
            "customer"
        )
        businesses = {b.pk: b for b in Business.objects.all()}

        for bill in bills:
            name = bill.bill_number
            lines = list(CustomerBillLine.objects.filter(bill=bill).select_related("delivery", "sale_line__sale"))
            if sum((l.amount for l in lines), ZERO) != bill.total_amount and bill.bill_type == "PERIOD":
                problems.append(f"{name}: total differs from the sum of its lines")
            for line in lines:
                d = line.delivery
                if d is None:
                    s = line.sale_line
                    if (s.line_total, s.quantity, s.unit_price, s.sale.customer_id) != (
                        line.amount, line.quantity, line.unit_price, bill.customer_id,
                    ):
                        problems.append(f"{name}: line for sale {s.sale.sale_number} no longer matches it")
                    if s.sale.status != "COMPLETED" or s.sale.payment_mode != "CREDIT":
                        problems.append(f"{name}: sale {s.sale.sale_number} was cancelled after billing")
                    continue
                if (d.line_total, d.quantity, d.unit_price, d.customer_id) != (
                    line.amount, line.quantity, line.unit_price, bill.customer_id,
                ):
                    problems.append(f"{name}: line for delivery {d.pk} no longer matches it")
                if d.status != "DELIVERED":
                    problems.append(f"{name}: delivery {d.pk} was cancelled after billing")
            if bill.bill_type == "PERIOD":
                live = set(
                    billable_deliveries(
                        businesses[bill.business_id], bill.customer_id,
                        bill.period_start, bill.period_end,
                    ).values_list("pk", flat=True)
                )
                billed = {l.delivery_id for l in lines}
                if live - billed:
                    problems.append(f"{name}: {len(live - billed)} delivery(ies) in the period are not on the bill")
                live_sales = set(
                    billable_sale_lines(
                        businesses[bill.business_id], bill.customer_id,
                        bill.period_start, bill.period_end,
                    ).values_list("pk", flat=True)
                )
                billed_sales = {l.sale_line_id for l in lines if l.sale_line_id}
                if live_sales - billed_sales:
                    problems.append(f"{name}: {len(live_sales - billed_sales)} shop sale line(s) in the period are not on the bill")
            clash = bills.filter(
                customer_id=bill.customer_id, bill_type="PERIOD",
                period_start__lte=bill.period_end, period_end__gte=bill.period_start,
            ).exclude(pk=bill.pk)
            if bill.bill_type == "PERIOD" and clash.exists():
                problems.append(f"{name}: overlaps another finalized bill")
            paid = selectors.allocated_to_bill(bill.business_id, bill)
            if paid > bill.total_amount:
                problems.append(f"{name}: allocated {paid} exceeds the total {bill.total_amount}")

        for payment in CustomerPayment.objects.all():
            amounts = selectors.get_payment_amounts(payment)
            if amounts["allocated"] > amounts["net"] or amounts["net"] < 0:
                problems.append(f"{payment.payment_number}: allocations exceed what the payment is worth")

        for customer_id, business_id in (
            CustomerBill.objects.values_list("customer_id", "business_id").distinct()
        ):
            p = selectors.get_customer_position(business_id=business_id, customer_id=customer_id)
            if p["net_balance"] != p["outstanding_amount"] - p["available_credit"]:
                problems.append(f"customer {customer_id}: balance does not reconcile")

        self.stdout.write(f"Finalized bills checked: {bills.count()}")
        for problem in problems:
            self.stdout.write(self.style.ERROR(problem))
        if problems:
            raise CommandError(f"{len(problems)} problem(s) found.")
        self.stdout.write(self.style.SUCCESS("Bills are consistent."))
