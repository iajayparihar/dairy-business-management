from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.inventory.models import StockMovement
from apps.inventory.services import get_stock_balance
from apps.payments.selectors import get_supplier_balance
from apps.payments.services import allocate_supplier_advance
from apps.payments.statements import get_supplier_account_statement
from apps.purchases.adjustments import correct_purchase_entry, return_purchase_goods
from apps.purchases.models import PurchaseAdjustment, SupplierPurchase
from tests.purchases.test_purchase_api import PurchaseTestBase


class AdjustmentBase(PurchaseTestBase):
    def setUp(self):
        super().setUp()
        self.purchase = self.received(qty="10.000", price="50.00")  # 500.00
        self.line = self.purchase.lines.get()

    def ret(self, qty, **kw):
        kw.setdefault("user", self.owner)
        kw.setdefault("reason", "damaged")
        return return_purchase_goods(
            business_id=self.business.pk, purchase_id=self.purchase.pk,
            lines=[{"purchase_line_id": self.line.pk, "quantity": qty}], **kw,
        )

    def fix(self, **fields):
        return correct_purchase_entry(
            business_id=self.business.pk, purchase_id=self.purchase.pk,
            user=self.owner, reason="typo",
            lines=[{"purchase_line_id": self.line.pk, **fields}],
        )

    def stock(self):
        return get_stock_balance(
            business_id=self.business.pk, product_id=self.tracked.pk,
            user=self.owner,
        )

    def balance(self):
        return get_supplier_balance(
            business_id=self.business.pk, supplier_id=self.supplier.pk
        )


class ReturnTests(AdjustmentBase):
    def test_partial_return_reverses_stock_and_reduces_payable(self):
        adj = self.ret("4.000")
        self.assertEqual(adj.amount, Decimal("-200.00"))
        self.assertEqual(self.stock(), Decimal("6.000"))
        movement = adj.lines.get().stock_movement
        self.assertEqual(movement.movement_type, "REVERSAL_OUT")
        self.assertEqual(movement.reversal_of, self.receipts(self.purchase).get())
        self.assertEqual(self.purchase.net_amount, Decimal("300.00"))
        self.assertEqual(self.balance()["total_purchases"], Decimal("300.00"))

    def test_original_records_untouched(self):
        self.ret("4.000")
        self.purchase.refresh_from_db()
        self.line.refresh_from_db()
        self.assertEqual(self.purchase.total_amount, Decimal("500.00"))
        self.assertEqual(self.line.quantity, Decimal("10.000"))
        self.assertEqual(self.line.line_total, Decimal("500.00"))
        self.assertEqual(self.receipts(self.purchase).get().quantity, Decimal("10.000"))

    def test_multiple_partial_returns_then_full_then_over_return(self):
        self.ret("4.000")
        self.ret("6.000")  # remaining 6 -> 0
        self.assertEqual(self.stock(), Decimal("0.000"))
        self.assertEqual(self.purchase.net_amount, Decimal("0.00"))
        with self.assertRaises(ValidationError):
            self.ret("0.001")
        self.assertEqual(PurchaseAdjustment.objects.count(), 2)

    def test_over_return_rejected_atomically(self):
        with self.assertRaises(ValidationError):
            self.ret("10.001")
        self.assertEqual(PurchaseAdjustment.objects.count(), 0)
        self.assertEqual(self.stock(), Decimal("10.000"))

    def test_duplicate_client_ref_rejected(self):
        self.ret("1.000", client_ref="req-1")
        with self.assertRaises(ValidationError):
            self.ret("1.000", client_ref="req-1")
        self.assertEqual(self.stock(), Decimal("9.000"))

    def test_untracked_product_adjusts_payable_only(self):
        purchase = self.received("PUR-U", product=self.untracked)
        line = purchase.lines.get()
        adj = return_purchase_goods(
            business_id=self.business.pk, purchase_id=purchase.pk,
            user=self.owner, reason="bad",
            lines=[{"purchase_line_id": line.pk, "quantity": "2"}],
        )
        self.assertEqual(adj.amount, Decimal("-100.00"))
        self.assertIsNone(adj.lines.get().stock_movement)
        self.assertFalse(StockMovement.objects.filter(product=self.untracked).exists())

    def test_draft_cancelled_and_reason_rules(self):
        draft = self.draft("PUR-D")
        with self.assertRaises(ValidationError):
            return_purchase_goods(
                business_id=self.business.pk, purchase_id=draft.pk,
                user=self.owner, reason="x",
                lines=[{"purchase_line_id": draft.lines.get().pk, "quantity": "1"}],
            )
        with self.assertRaises(ValidationError):
            self.ret("1", reason="  ")

    def test_line_from_other_purchase_rejected(self):
        other = self.received("PUR-2")
        with self.assertRaises(ValidationError):
            return_purchase_goods(
                business_id=self.business.pk, purchase_id=self.purchase.pk,
                user=self.owner, reason="x",
                lines=[{"purchase_line_id": other.lines.get().pk, "quantity": "1"}],
            )

    def test_rollback_when_stock_posting_fails(self):
        with mock.patch.object(
            StockMovement.objects, "create", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.ret("2.000")
        self.assertEqual(PurchaseAdjustment.objects.count(), 0)
        self.assertEqual(self.stock(), Decimal("10.000"))

    def test_permissions_and_tenant(self):
        for user in (self.staff, self.outsider):
            with self.assertRaises(PermissionDenied):
                self.ret("1", user=user)
        with self.assertRaises(SupplierPurchase.DoesNotExist):
            return_purchase_goods(
                business_id=self.other_business.pk, purchase_id=self.purchase.pk,
                user=self.outsider, reason="x",
                lines=[{"purchase_line_id": self.line.pk, "quantity": "1"}],
            )

    def test_records_are_immutable(self):
        adj = self.ret("1")
        adj.reason = "changed"
        with self.assertRaises(ValueError):
            adj.save()
        with self.assertRaises(ValueError):
            adj.delete()
        with self.assertRaises(ValueError):
            adj.lines.get().delete()


class CorrectionTests(AdjustmentBase):
    def test_price_correction_changes_payable_not_stock(self):
        adj = self.fix(unit_price="45.00")
        self.assertEqual(adj.amount, Decimal("-50.00"))
        self.assertEqual(self.stock(), Decimal("10.000"))
        self.assertIsNone(adj.lines.get().stock_movement)
        line = adj.lines.get()
        self.assertEqual(line.unit_price_before, Decimal("50.0000"))
        self.assertEqual(line.unit_price_after, Decimal("45.0000"))
        self.line.refresh_from_db()
        self.assertEqual(self.line.unit_price, Decimal("50.0000"))

    def test_quantity_correction_down_and_up(self):
        down = self.fix(quantity="8.000")
        self.assertEqual(down.amount, Decimal("-100.00"))
        self.assertEqual(self.stock(), Decimal("8.000"))
        up = self.fix(quantity="12.000")
        self.assertEqual(up.amount, Decimal("200.00"))
        self.assertEqual(up.lines.get().stock_movement.movement_type, "ADJUSTMENT_IN")
        self.assertEqual(self.stock(), Decimal("12.000"))
        self.assertEqual(self.purchase.net_amount, Decimal("600.00"))

    def test_return_after_price_correction_uses_corrected_price(self):
        self.fix(unit_price="40.00")  # 400
        adj = self.ret("5.000")
        self.assertEqual(adj.amount, Decimal("-200.00"))
        self.assertEqual(self.purchase.net_amount, Decimal("200.00"))

    def test_noop_and_empty_corrections_rejected(self):
        for fields in ({}, {"quantity": "10.000"}, {"unit_price": "50"},
                       {"quantity": "0"}, {"unit_price": "-1"}):
            with self.assertRaises(ValidationError, msg=fields):
                self.fix(**fields)
        self.assertEqual(PurchaseAdjustment.objects.count(), 0)

    def test_correction_rollback(self):
        with mock.patch.object(
            StockMovement.objects, "create", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.fix(quantity="5.000")
        self.assertEqual(PurchaseAdjustment.objects.count(), 0)


class LedgerInteractionTests(AdjustmentBase):
    def test_allocation_capped_by_net_payable(self):
        self.ret("4.000")  # net 300
        payment = self.pay_advance("500.00")
        with self.assertRaises(ValidationError):
            allocate_supplier_advance(
                payment_id=payment.pk, purchase_id=self.purchase.pk,
                amount="300.01", user=self.owner,
            )
        allocate_supplier_advance(
            payment_id=payment.pk, purchase_id=self.purchase.pk,
            amount="300.00", user=self.owner,
        )
        bal = self.balance()
        self.assertEqual(bal["outstanding_amount"], Decimal("0.00"))
        self.assertEqual(bal["available_credit"], Decimal("200.00"))

    def test_return_after_full_payment_leaves_supplier_credit(self):
        payment = self.pay_advance("500.00")
        allocate_supplier_advance(
            payment_id=payment.pk, purchase_id=self.purchase.pk,
            amount="500.00", user=self.owner,
        )
        self.ret("4.000")  # we now owe -200
        stmt = self.statement()
        self.assertEqual(stmt["closing_balance"], Decimal("-200.00"))
        # No further allocation to an already over-paid purchase.
        other = self.pay_advance("10.00", number="ADV-2")
        with self.assertRaises(ValidationError):
            allocate_supplier_advance(
                payment_id=other.pk, purchase_id=self.purchase.pk,
                amount="1.00", user=self.owner,
            )

    def test_statement_shows_adjustment_and_history_is_preserved(self):
        before = self.statement()
        self.assertEqual(before["closing_balance"], Decimal("500.00"))
        self.ret("4.000")
        stmt = self.statement()
        self.assertEqual(
            [t["type"] for t in stmt["transactions"]],
            ["PURCHASE", "PURCHASE_RETURN"],
        )
        self.assertEqual(stmt["closing_balance"], Decimal("300.00"))
        self.assertEqual(stmt["total_purchases"], self.balance()["total_purchases"])

        # A statement as of yesterday is unchanged by today's return.
        past = get_supplier_account_statement(
            business_id=self.business.pk, supplier_id=self.supplier.pk,
            end_date=timezone.localdate() - timedelta(days=1),
        )
        self.assertEqual(past["transactions"], [])

        # Opening balance of tomorrow's period includes the adjustment.
        nxt = get_supplier_account_statement(
            business_id=self.business.pk, supplier_id=self.supplier.pk,
            start_date=timezone.localdate() + timedelta(days=1),
            end_date=timezone.localdate() + timedelta(days=2),
        )
        self.assertEqual(nxt["opening_balance"], Decimal("300.00"))

    def test_payment_api_auto_allocation_uses_net(self):
        self.ret("4.000")
        client = APIClient()
        client.force_authenticate(self.owner)
        res = client.post(
            reverse("payments:supplier-payment-list-create", kwargs={
                "business_id": self.business.pk, "supplier_id": self.supplier.pk,
            }),
            {"payment_number": "P-API", "paid_at": timezone.now().isoformat(),
             "amount": "400.00", "payment_method": "UPI"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        bal = self.balance()
        self.assertEqual(bal["allocated_payments"], Decimal("300.00"))
        self.assertEqual(bal["available_credit"], Decimal("100.00"))

    def pay_advance(self, amount, number="ADV-1"):
        from apps.payments.services import record_supplier_payment
        return record_supplier_payment(
            business_id=self.business.pk, supplier_id=self.supplier.pk,
            payment_number=number, paid_at=timezone.now(), amount=amount,
            user=self.owner,
        )

    def statement(self):
        return get_supplier_account_statement(
            business_id=self.business.pk, supplier_id=self.supplier.pk
        )


class AdjustmentAPITests(AdjustmentBase):
    def url(self, name, business=None):
        return reverse(f"purchases:{name}", kwargs={
            "business_id": (business or self.business).pk,
            "pk": self.purchase.pk,
        })

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def body(self, **over):
        data = {"reason": "damaged",
                "lines": [{"purchase_line_id": str(self.line.pk), "quantity": "3.000"}]}
        data.update(over)
        return data

    def test_return_and_history_endpoint(self):
        client = self.client_for(self.manager)
        res = client.post(self.url("purchase-return"), self.body(), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data["amount"], "-150.00")

        res = client.get(self.url("purchase-adjustments"))
        self.assertEqual(res.data["original_total"], Decimal("500.00"))
        self.assertEqual(res.data["net_amount"], Decimal("350.00"))
        self.assertEqual(len(res.data["adjustments"]), 1)

    def test_correction_endpoint(self):
        res = self.client_for(self.owner).post(
            self.url("purchase-correction"),
            {"reason": "typo", "lines": [
                {"purchase_line_id": str(self.line.pk), "unit_price": "48.0000"}]},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data["amount"], "-20.00")

    def test_over_return_and_bad_input_are_400(self):
        client = self.client_for(self.owner)
        for body in (
            self.body(lines=[{"purchase_line_id": str(self.line.pk), "quantity": "11"}]),
            self.body(reason=""),
            self.body(lines=[]),
        ):
            res = client.post(self.url("purchase-return"), body, format="json")
            self.assertEqual(res.status_code, 400, body)
        self.assertEqual(PurchaseAdjustment.objects.count(), 0)

    def test_staff_unauth_and_cross_tenant(self):
        self.assertEqual(
            self.client_for(self.staff).post(
                self.url("purchase-return"), self.body(), format="json"
            ).status_code, 403)
        self.assertEqual(
            self.client_for(self.staff).get(self.url("purchase-adjustments")).status_code,
            403)
        self.assertIn(
            APIClient().post(self.url("purchase-return"), self.body(), format="json"
                             ).status_code, (401, 403))
        outsider = self.client_for(self.outsider)
        self.assertEqual(
            outsider.post(self.url("purchase-return"), self.body(), format="json"
                          ).status_code, 403)
        for name in ("purchase-return", "purchase-correction"):
            self.assertEqual(
                outsider.post(self.url(name, self.other_business), self.body(),
                              format="json").status_code, 404)
        self.assertEqual(
            outsider.get(self.url("purchase-adjustments", self.other_business)
                         ).status_code, 404)
        self.assertEqual(PurchaseAdjustment.objects.count(), 0)
