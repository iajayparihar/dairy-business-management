"""
Customer money.

- Opening balances: two customers owed money before the history starts
  (OPENING bill), one had paid in advance (advance payment, method OTHER).
- Last month: a bill for every customer with deliveries / credit sales,
  generated and FINALIZED (the advance is applied automatically).
- One bill is voided after a dispute, a disputed delivery is cancelled, and
  the replacement bill is finalized.
- This month: most customers pay in full, some in part, some not at all;
  one pays extra (advance); one cheque bounces (payment reversal).
- This month so far: DRAFT bills for the shop customers (not finalized).
"""
from datetime import timedelta
from decimal import Decimal

from apps.billing import selectors
from apps.billing.bill_services import (
    NothingToBill,
    finalize_bill,
    generate_draft_bill,
    generate_drafts,
    record_opening_balance,
    void_bill,
)
from apps.billing.models import CustomerBill
from apps.billing.payment_services import record_customer_payment, reverse_customer_payment

from .base import at, history_start, is_past, money, rng, this_month, today
from .deliveries import cancel_route_delivery

METHODS = ["CASH", "CASH", "UPI", "UPI", "BANK_TRANSFER"]


def _due(t, bill):
    return bill.total_amount - selectors.allocated_to_bill(t.business.pk, bill)


def seed(t):
    biz, owner = t.business, t.owner
    start, first = history_start(), this_month()
    before = start - timedelta(days=1)
    customers = list(t.customers.values())

    for c in customers[1:3]:
        record_opening_balance(
            business_id=biz.pk, customer_id=c.pk, amount=money(rng.randint(8, 25) * 100),
            as_of_date=before, user=owner,
        )
    record_customer_payment(
        business_id=biz.pk, customer_id=customers[3].pk, amount=money(500),
        paid_at=at(before, 18), payment_method="OTHER", user=owner,
        notes="Advance carried over from the old notebook.",
    )

    last_end = first - timedelta(days=1)
    drafts = generate_drafts(
        business_id=biz.pk, period_start=start, period_end=last_end, user=owner,
    )["created"]
    for bill in drafts:
        finalize_bill(business_id=biz.pk, bill_id=bill.pk, user=owner)

    # Dispute: void, remove the disputed delivery, finalize the replacement.
    disputed = drafts[0]
    _, replacement = void_bill(
        business_id=biz.pk, bill_id=disputed.pk, user=owner,
        reason="Customer was out of town on one day; delivery was wrongly marked.",
    )
    delivery = disputed.customer.deliveries.filter(
        status="DELIVERED", delivered_at__gte=at(start, 0), delivered_at__lt=at(first, 0),
    ).first()
    if delivery:
        cancel_route_delivery(t, delivery, "Disputed on last month's bill: customer was out of town.")
    finalize_bill(business_id=biz.pk, bill_id=replacement.pk, user=owner)

    # Payments this month against last month's (and opening) bills.
    live = CustomerBill.objects.filter(business=biz, status="FINALIZED").select_related("customer")
    by_customer = {}
    for bill in live:
        by_customer.setdefault(bill.customer, []).append(bill)
    for i, (customer, bills) in enumerate(sorted(by_customer.items(), key=lambda kv: kv[0].customer_code)):
        paid_at = at(first + timedelta(days=rng.randint(1, 9)), rng.randint(8, 19))
        if not is_past(paid_at):
            continue
        dues = [(b, _due(t, b)) for b in bills]
        owed = sum((d for _, d in dues), money(0))
        roll = rng.random()
        if owed <= 0 or roll < 0.15:
            continue  # nothing owed, or simply has not paid yet
        amount = owed if roll > 0.4 else (money(round(owed * Decimal("0.6"), -2)) or owed)
        if i == 0:
            amount = owed + money(1000)  # pays extra: advance for next month
        allocations, left = [], amount
        for bill, due in dues:
            share = min(due, left)
            if share > 0:
                allocations.append({"bill_id": bill.pk, "amount": share})
                left -= share
        method = rng.choice(METHODS)
        record_customer_payment(
            business_id=biz.pk, customer_id=customer.pk, amount=amount,
            paid_at=paid_at, payment_method=method, user=t.manager,
            reference_number="" if method == "CASH" else f"REF{rng.randint(100000, 999999)}",
            allocations=allocations,
        )

    bounced = record_customer_payment(
        business_id=biz.pk, customer_id=t.credit_customer.pk, amount=money(1500),
        paid_at=at(first, 12) if is_past(at(first, 12)) else None,
        payment_method="CHEQUE", reference_number="CHQ 004512", user=t.manager,
        notes="Cheque for last month's khata",
    )
    reverse_customer_payment(
        business_id=biz.pk, payment_id=bounced.pk, user=owner,
        reason="Cheque bounced: insufficient funds.",
    )

    # This month so far: drafts only, for the shop customers.
    if today() > first:
        for c in customers:
            if c.customer_type == "SHOP" and c.is_active:
                try:
                    generate_draft_bill(
                        business_id=biz.pk, customer_id=c.pk, period_start=first,
                        period_end=today() - timedelta(days=1), user=owner,
                    )
                except NothingToBill:
                    pass
