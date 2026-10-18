"""Bulk positions must equal the per-party selectors for every party."""
from apps.billing.selectors import get_customer_position
from apps.billing.payment_services import reverse_customer_payment
from apps.payments.selectors import get_supplier_position
from apps.payments.services import reverse_supplier_payment
from apps.reports.positions import customer_positions, supplier_positions
from tests.reports.base import D, ReportBase, ist


class BulkPositionTests(ReportBase):
    def setUp(self):
        super().setUp()
        # Customer 1: finalized Sept bill 1240, 700 paid, then 200 reversed.
        self.september()
        self.finalize(self.draft())
        self.pay("700.00", paid_at=ist(2026, 10, 2))
        pay2 = self.pay("100.00", paid_at=ist(2026, 10, 3))
        reverse_customer_payment(
            business_id=self.business.pk, payment_id=pay2.pk,
            reason="bounced", user=self.owner, amount="50.00",
        )
        # Customer 2: a bill that gets voided with a payment against it.
        self.deliver(self.cow, "1", ist(2026, 9, 5), customer=self.customer2)
        bill2 = self.finalize(self.draft(customer=self.customer2))
        self.pay("40.00", customer=self.customer2, paid_at=ist(2026, 10, 2))
        self.void(bill2)
        # Suppliers: purchases, advance payment, partial reversal, cancel.
        self.milk(self.s1, 100, ist(2026, 9, 3))        # 1000
        c = self.milk(self.s1, 50, ist(2026, 9, 4))     # 500
        p = self.spay(self.s1, 1200, ist(2026, 9, 10), auto_allocate=True)
        reverse_supplier_payment(
            payment_id=p.pk, amount="300.00", reason="refund", user=self.owner,
        )
        from apps.purchases.services import cancel_milk_collection
        cancel_milk_collection(
            business_id=self.business.pk, collection_id=c.pk,
            user=self.owner, reason="wrong can",
        )
        self.spay(self.s2, 80, ist(2026, 9, 11))        # pure advance

    def check_customers(self, as_of):
        bulk = customer_positions(self.business.pk, as_of)
        for c in (self.customer, self.customer2):
            single = get_customer_position(
                business_id=self.business.pk, customer_id=c.pk, as_of=as_of
            )
            got = bulk.get(c.pk)
            if got is None:
                self.assertTrue(all(v == 0 for v in single.values()), c.name)
            else:
                self.assertEqual(got, single, c.name)

    def check_suppliers(self, as_of):
        bulk = supplier_positions(self.business.pk, as_of)
        for s in (self.s1, self.s2):
            single = get_supplier_position(
                business_id=self.business.pk, supplier_id=s.pk, as_of=as_of
            )
            got = bulk.get(s.pk)
            if got is None:
                self.assertTrue(all(v == 0 for v in single.values()), s.name)
            else:
                self.assertEqual(got, single, s.name)

    def test_customers_match_per_customer_selector_now(self):
        self.check_customers(None)

    def test_suppliers_match_per_supplier_selector_now(self):
        self.check_suppliers(None)

    def test_match_at_past_instants(self):
        for as_of in (ist(2026, 9, 5), ist(2026, 9, 20), ist(2026, 10, 2, 12)):
            self.check_customers(as_of)
            self.check_suppliers(as_of)

    def test_known_supplier_numbers(self):
        # 1000 live purchase (500 cancelled), paid 1200-300=900, 900 allocated.
        pos = supplier_positions(self.business.pk)
        self.assertEqual(pos[self.s1.pk]["total_purchases"], D("1000.00"))
        self.assertEqual(pos[self.s1.pk]["total_payments"], D("900.00"))
        self.assertEqual(pos[self.s2.pk]["available_credit"], D("80.00"))

    def test_other_business_is_invisible(self):
        self.assertEqual(customer_positions(self.other_business.pk), {})
        self.assertEqual(supplier_positions(self.other_business.pk), {})

    def test_query_count_does_not_grow_with_parties(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as small:
            customer_positions(self.business.pk)
            supplier_positions(self.business.pk)
        for i in range(5):
            self.supplier(f"X{i}", f"X{i}")
            self.milk(self.supplier(f"Y{i}", f"Y{i}"), 10, ist(2026, 9, 6))
        with CaptureQueriesContext(connection) as big:
            customer_positions(self.business.pk)
            supplier_positions(self.business.pk)
        self.assertEqual(len(small), len(big))
