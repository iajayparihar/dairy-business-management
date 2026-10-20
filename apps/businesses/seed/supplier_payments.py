"""
Supplier payments.

Farmers are paid every ten days (1-10, 11-20, 21-month end), two days after
the cycle closes, for exactly what they delivered in it (auto-allocated to
the oldest open milk purchases). The running cycle stays unpaid, so
outstanding balances show up. One farmer takes an advance; one payment is
partly reversed. The distributor is paid for last month's invoices.
"""
from calendar import monthrange
from datetime import timedelta

from django.db.models import Sum

from apps.payments.models import SupplierPayment
from apps.payments.services import record_supplier_payment, reverse_supplier_payment
from apps.purchases.models import MilkCollection, SupplierPurchase

from .base import at, history_start, is_past, money, rng, this_month, today


def _cycles():
    """(first, last) day of every closed ten-day cycle."""
    day = history_start()
    while True:
        end_day = 10 if day.day == 1 else 20 if day.day == 11 else monthrange(day.year, day.month)[1]
        end = day.replace(day=end_day)
        if end >= today():
            return
        yield day, end
        day = end + timedelta(days=1)


def seed(t):
    biz, counter = t.business, {}

    def pay(supplier, amount, paid_at, **extra):
        counter[supplier.pk] = counter.get(supplier.pk, 0) + 1
        method = extra.pop("payment_method", rng.choice(["CASH", "CASH", "UPI", "BANK_TRANSFER"]))
        return record_supplier_payment(
            business_id=biz.pk, supplier_id=supplier.pk, amount=amount, user=t.owner,
            payment_number=f"SP-{supplier.supplier_code}-{counter[supplier.pk]:03d}",
            paid_at=paid_at, payment_method=method,
            reference_number="" if method == "CASH" else f"UTR{rng.randint(10**9, 10**10 - 1)}",
            **extra,
        )

    for first, last in _cycles():
        paid_at = at(last + timedelta(days=2), 10)
        if not is_past(paid_at):
            continue
        for supplier, _ in t.farmers:
            amount = MilkCollection.objects.filter(
                business=biz, supplier=supplier, status="RECORDED",
                collected_at__gte=at(first, 0), collected_at__lt=at(last + timedelta(days=1), 0),
            ).aggregate(s=Sum("amount"))["s"]
            if amount:
                pay(supplier, amount, paid_at, auto_allocate=True,
                    notes=f"Milk payment {first:%d %b} - {last:%d %b}")

    farmer = t.farmers[0][0]
    pay(farmer, money(2000), at(today() - timedelta(days=3), 18),
        payment_method="CASH", notes="Advance: buffalo treatment at the vet.")

    overpaid = SupplierPayment.objects.filter(
        business=biz, supplier=farmer, notes__startswith="Milk payment",
    ).order_by("-paid_at").first()
    if overpaid:
        reverse_supplier_payment(
            payment_id=overpaid.pk, business_id=biz.pk, user=t.owner,
            amount=money(500), reason="Paid Rs. 500 too much; farmer returned it.",
        )

    last_month = SupplierPurchase.objects.filter(
        business=biz, supplier=t.distributor, status="RECEIVED",
        purchased_at__lt=at(this_month(), 0),
    ).aggregate(s=Sum("total_amount"))["s"]
    if last_month and is_past(at(this_month() + timedelta(days=4), 11)):
        pay(t.distributor, last_month, at(this_month() + timedelta(days=4), 11),
            payment_method="BANK_TRANSFER", auto_allocate=True,
            notes="Last month's invoices")
