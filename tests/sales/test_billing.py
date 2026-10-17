"""Shop credit sales on the monthly bill, the balance and the period freeze."""
from django.core.exceptions import ValidationError
from django.core.management import call_command

from apps.billing.bill_services import generate_drafts
from apps.billing.documents import bill_document
from apps.billing.guards import customer_has_records
from apps.billing.models import CustomerBill
from apps.billing.pdf import render_bill_pdf
from apps.billing.statements import get_customer_balance
from apps.sales.services import cancel_sale, correct_sale
from tests.billing.base import SEP, ist
from tests.sales.base import D, SalesBase

PANEER = lambda self: [{"product_id": self.paneer.pk, "quantity": "0.250"}]


class SaleOnBillTests(SalesBase):
    def credit(self, day=10, lines=None, customer=None, **kw):
        kw["customer_id"] = (customer or self.customer).pk
        return self.sell(lines or PANEER(self), mode="CREDIT",
                         sold_at=ist(2026, 9, day, 11), user=self.owner, **kw)

    def test_credit_sale_joins_milk_on_one_bill(self):
        self.september()  # 1240.00 of milk
        self.credit()  # 0.25 kg x 400 = 100.00
        self.credit(day=11, lines=[{"description": "Bread", "amount": "35"}])
        bill = self.finalize(self.draft())
        self.assertEqual(bill.total_amount, D("1375.00"))
        lines = self.lines(bill)
        self.assertEqual(len(lines), 7)
        shop = [l for l in lines if l.sale_line_id]
        self.assertEqual(len(shop), 2)
        self.assertTrue(all(l.delivery_id is None and l.shift == "" for l in shop))
        other = [l for l in shop if l.product_id is None]
        self.assertEqual(other[0].description, "Bread")
        self.assertEqual(self.position()["net_balance"], D("1375.00"))
        self.assert_reconciled(self.customer)

    def test_cash_and_online_sales_never_billed(self):
        self.september()
        self.sell(PANEER(self), mode="CASH", sold_at=ist(2026, 9, 10),
                  user=self.owner, customer_id=self.customer.pk)
        self.sell(PANEER(self), mode="ONLINE", sold_at=ist(2026, 9, 10),
                  user=self.owner)
        bill = self.finalize(self.draft())
        self.assertEqual(bill.total_amount, D("1240.00"))
        self.assertEqual(get_customer_balance(
            business_id=self.business.pk, customer_id=self.customer.pk,
        )["unbilled_sales"], 0)

    def test_sale_only_customer_can_be_billed_and_bulk_drafted(self):
        self.credit(customer=self.customer2)
        result = generate_drafts(
            business_id=self.business.pk, period_start=SEP[0],
            period_end=SEP[1], user=self.owner,
        )
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["created"][0].customer, self.customer2)

    def test_nothing_to_bill_without_sales_or_deliveries(self):
        from apps.billing.bill_services import NothingToBill
        with self.assertRaises(NothingToBill):
            self.draft()

    def test_unbilled_report_and_balance(self):
        self.credit()
        bal = get_customer_balance(
            business_id=self.business.pk, customer_id=self.customer.pk)
        self.assertEqual(bal["unbilled_amount"], D("100.00"))
        self.assertEqual(bal["unbilled_sales"], 1)
        self.assertEqual(bal["net_balance"], D("0.00"))  # never counted
        self.finalize(self.draft())
        bal = get_customer_balance(
            business_id=self.business.pk, customer_id=self.customer.pk)
        self.assertEqual((bal["unbilled_amount"], bal["unbilled_sales"]),
                         (D("0"), 0))
        self.assertEqual(bal["net_balance"], D("100.00"))

    def test_cancelled_sale_not_billed(self):
        gone = self.credit()
        self.credit(day=12)
        cancel_sale(business_id=self.business.pk, sale_id=gone.pk,
                    user=self.owner, reason="x")
        self.assertEqual(self.finalize(self.draft()).total_amount, D("100.00"))

    def test_document_and_pdf_show_shop_lines(self):
        self.credit()
        draft = self.draft()
        self.assertEqual(bill_document(draft)["lines"][0]["source"], "SHOP")
        bill = self.finalize(draft)
        doc = bill_document(bill)
        self.assertEqual([l["source"] for l in doc["lines"]], ["SHOP"])
        self.assertEqual(doc["totals"]["total"], "100.00")
        self.assertTrue(render_bill_pdf(bill).startswith(b"%PDF"))

    def test_milk_product_sold_in_shop_counts_as_milk(self):
        self.credit(lines=[{"product_id": self.cow.pk, "quantity": "2"}])
        doc = bill_document(self.finalize(self.draft()))
        self.assertEqual(doc["totals"]["cow_litres"], "2.000")

    def test_audit_clean_then_detects_missing_sale_line(self):
        self.september()
        self.credit()
        bill = self.finalize(self.draft())
        call_command("audit_bills")
        # A credit sale added behind the guards (bypassing the service).
        from apps.sales.models import Sale, SaleLine
        sale = Sale.objects.create(
            business=self.business, sale_number="SHOP-X-1",
            customer=self.customer, payment_mode="CREDIT",
            sold_at=ist(2026, 9, 20), total_amount=D("5"), created_by=self.owner,
        )
        SaleLine.objects.create(
            business=self.business, sale=sale, position=1, description="x",
            quantity=1, unit_price=D("5"),
        )
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("audit_bills")

    def test_customer_with_only_a_sale_cannot_be_deleted(self):
        self.assertFalse(customer_has_records(self.customer2))
        self.credit(customer=self.customer2)
        self.assertTrue(customer_has_records(self.customer2))


class PeriodFreezeTests(SalesBase):
    def setUp(self):
        super().setUp()
        self.sale = self.sell(PANEER(self), mode="CREDIT",
                              customer_id=self.customer.pk,
                              sold_at=ist(2026, 9, 10), user=self.owner)
        self.bill = self.finalize(self.draft())

    def test_credit_sale_in_billed_period_refused(self):
        with self.assertRaisesMessage(ValidationError, self.bill.bill_number):
            self.sell(PANEER(self), mode="CREDIT",
                      customer_id=self.customer.pk,
                      sold_at=ist(2026, 9, 15), user=self.owner)

    def test_cash_sale_in_billed_period_is_fine(self):
        self.sell(PANEER(self), mode="CASH", sold_at=ist(2026, 9, 15),
                  user=self.owner, customer_id=self.customer.pk)

    def test_cancel_and_correct_credit_refused_until_void(self):
        with self.assertRaisesMessage(ValidationError, self.bill.bill_number):
            cancel_sale(business_id=self.business.pk, sale_id=self.sale.pk,
                        user=self.owner, reason="x")
        with self.assertRaisesMessage(ValidationError, self.bill.bill_number):
            correct_sale(
                business_id=self.business.pk, sale_id=self.sale.pk,
                user=self.owner, reason="x", payment_mode="CREDIT",
                lines=[{"description": "a", "amount": "1"}],
            )
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.status, "COMPLETED")

    def test_void_then_correct_flows_into_replacement_draft(self):
        self.void(self.bill)
        new = correct_sale(
            business_id=self.business.pk, sale_id=self.sale.pk,
            user=self.owner, reason="wrong qty", payment_mode="CREDIT",
            lines=[{"product_id": self.paneer.pk, "quantity": "0.5"}],
        )
        replacement = CustomerBill.objects.get(status="DRAFT")
        self.assertEqual(replacement.replaces, self.bill)
        self.assertEqual(self.finalize(replacement).total_amount, D("200.00"))
        self.assertEqual(new.total_amount, D("200.00"))

    def test_void_unbills_the_sale(self):
        self.void(self.bill)
        bal = get_customer_balance(
            business_id=self.business.pk, customer_id=self.customer.pk)
        self.assertEqual(bal["unbilled_sales"], 1)
        self.assertEqual(bal["net_balance"], D("0.00"))
