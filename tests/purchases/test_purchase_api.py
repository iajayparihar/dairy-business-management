from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.inventory.models import StockMovement
from apps.inventory.services import get_stock_balance
from apps.payments.models import SupplierPayment, SupplierPaymentAllocation
from apps.payments.selectors import get_supplier_balance
from apps.payments.services import (
    allocate_supplier_advance,
    record_supplier_payment,
    reverse_supplier_payment,
)
from apps.payments.statements import get_supplier_account_statement
from apps.products.models import Product
from apps.purchases.models import SupplierPurchase
from apps.purchases.services import (
    cancel_draft_purchase,
    create_purchase,
    receive_purchase,
    update_draft_purchase,
)
from apps.suppliers.models import Supplier

User = get_user_model()


class PurchaseTestBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pw-123456")
        self.manager = User.objects.create_user("manager", password="pw-123456")
        self.staff = User.objects.create_user("staff", password="pw-123456")
        self.outsider = User.objects.create_user("outsider", password="pw-123456")

        self.business = create_business(
            name="Dairy A", slug="dairy-a", owner=self.owner
        )
        for user, role in ((self.manager, "MANAGER"), (self.staff, "STAFF")):
            BusinessMembership.objects.create(
                business=self.business, user=user, role=role, is_active=True
            )

        self.other_business = create_business(
            name="Dairy B", slug="dairy-b", owner=self.outsider
        )

        self.supplier = Supplier.objects.create(
            business=self.business, supplier_code="S1", name="Sup 1"
        )
        self.supplier2 = Supplier.objects.create(
            business=self.business, supplier_code="S2", name="Sup 2"
        )
        self.foreign_supplier = Supplier.objects.create(
            business=self.other_business, supplier_code="F1", name="Foreign"
        )
        self.tracked = Product.objects.create(
            business=self.business, sku="P1", name="Cream",
            unit="LITRE", track_stock=True,
        )
        self.untracked = Product.objects.create(
            business=self.business, sku="P2", name="Service",
            unit="LITRE", track_stock=False,
        )
        self.foreign_product = Product.objects.create(
            business=self.other_business, sku="FP", name="Foreign",
            unit="LITRE", track_stock=True,
        )

    def draft(self, number="PUR-1", qty="10.000", price="50.00",
              supplier=None, product=None):
        return create_purchase(
            business_id=self.business.pk,
            supplier_id=(supplier or self.supplier).pk,
            purchase_number=number,
            purchased_at=timezone.now(),
            lines=[{
                "product_id": (product or self.tracked).pk,
                "quantity": qty,
                "unit_price": price,
            }],
            user=self.owner,
        )

    def received(self, number="PUR-1", **kw):
        purchase = self.draft(number, **kw)
        receive_purchase(purchase_id=purchase.pk, user=self.owner)
        purchase.refresh_from_db()
        return purchase

    def receipts(self, purchase):
        return StockMovement.objects.filter(
            purchase_line__purchase=purchase,
            movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
        )


class PurchaseLifecycleServiceTests(PurchaseTestBase):
    def test_update_draft_replaces_lines_and_total(self):
        purchase = self.draft()
        update_draft_purchase(
            purchase_id=purchase.pk, user=self.owner,
            lines=[{"product_id": self.tracked.pk, "quantity": "2", "unit_price": "10"}],
        )
        purchase.refresh_from_db()
        self.assertEqual(purchase.total_amount, Decimal("20.00"))
        self.assertEqual(purchase.lines.count(), 1)

    def test_cancel_draft_records_audit_and_touches_nothing_else(self):
        purchase = self.draft()
        cancel_draft_purchase(
            purchase_id=purchase.pk, user=self.manager, reason=" typo "
        )
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, SupplierPurchase.Status.CANCELLED)
        self.assertEqual(purchase.cancelled_by, self.manager)
        self.assertIsNotNone(purchase.cancelled_at)
        self.assertEqual(purchase.cancel_reason, "typo")
        self.assertEqual(purchase.lines.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 0)
        self.assertEqual(SupplierPayment.objects.count(), 0)

    def test_cancelled_draft_cannot_be_received_cancelled_or_edited(self):
        purchase = self.draft()
        cancel_draft_purchase(purchase_id=purchase.pk, user=self.owner, reason="test")
        for call in (
            lambda: receive_purchase(purchase_id=purchase.pk, user=self.owner),
            lambda: cancel_draft_purchase(purchase_id=purchase.pk, user=self.owner, reason="test"),
            lambda: update_draft_purchase(
                purchase_id=purchase.pk, user=self.owner, notes="x"
            ),
        ):
            with self.assertRaises(ValidationError):
                call()
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_received_purchase_cannot_be_cancelled_or_edited(self):
        purchase = self.received()
        with self.assertRaises(ValidationError):
            cancel_draft_purchase(purchase_id=purchase.pk, user=self.owner, reason="test")
        with self.assertRaises(ValidationError):
            update_draft_purchase(
                purchase_id=purchase.pk, user=self.owner, notes="x"
            )
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, SupplierPurchase.Status.RECEIVED)
        self.assertEqual(purchase.total_amount, Decimal("500.00"))

    def test_invalid_quantity_and_price_rejected(self):
        for qty, price in (("0", "1"), ("-1", "1"), ("1.0001", "1"),
                           ("1", "-1"), ("1", "1.00001"), ("abc", "1")):
            with self.assertRaises(ValidationError, msg=(qty, price)):
                self.draft(number=f"X-{qty}-{price}", qty=qty, price=price)
        self.assertEqual(SupplierPurchase.objects.count(), 0)

    def test_foreign_supplier_and_product_rejected(self):
        with self.assertRaises(Supplier.DoesNotExist):
            self.draft(supplier=self.foreign_supplier)
        with self.assertRaises(Product.DoesNotExist):
            self.draft(product=self.foreign_product)
        self.assertEqual(SupplierPurchase.objects.count(), 0)

    def test_staff_and_outsider_denied(self):
        from django.core.exceptions import PermissionDenied
        purchase = self.draft()
        for user in (self.staff, self.outsider):
            with self.assertRaises(PermissionDenied):
                receive_purchase(purchase_id=purchase.pk, user=user)
            with self.assertRaises(PermissionDenied):
                cancel_draft_purchase(purchase_id=purchase.pk, user=user)

    def test_business_scoped_lookup_hides_foreign_purchase(self):
        purchase = self.draft()
        with self.assertRaises(SupplierPurchase.DoesNotExist):
            receive_purchase(
                purchase_id=purchase.pk, user=self.outsider,
                business_id=self.other_business.pk,
            )


class PurchaseInventoryTests(PurchaseTestBase):
    def test_tracked_receipt_posts_exact_quantity(self):
        purchase = self.received(qty="12.500")
        movement = self.receipts(purchase).get()
        self.assertEqual(movement.quantity, Decimal("12.500"))
        self.assertEqual(
            get_stock_balance(
                business_id=self.business.pk,
                product_id=self.tracked.pk, user=self.owner,
            ),
            Decimal("12.500"),
        )

    def test_untracked_product_posts_no_stock(self):
        purchase = self.received(product=self.untracked)
        self.assertEqual(purchase.status, SupplierPurchase.Status.RECEIVED)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_repeat_receipt_does_not_duplicate_stock(self):
        purchase = self.received()
        with self.assertRaises(ValidationError):
            receive_purchase(purchase_id=purchase.pk, user=self.owner)
        self.assertEqual(self.receipts(purchase).count(), 1)

    def test_failure_mid_receipt_rolls_back_everything(self):
        purchase = create_purchase(
            business_id=self.business.pk,
            supplier_id=self.supplier.pk,
            purchase_number="PUR-RB",
            purchased_at=timezone.now(),
            lines=[
                {"product_id": self.tracked.pk, "quantity": "1", "unit_price": "1"},
                {"product_id": self.tracked.pk, "quantity": "2", "unit_price": "1"},
            ],
            user=self.owner,
        )
        real_create = StockMovement.objects.create
        calls = []

        def fail_second(**kw):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return real_create(**kw)

        with mock.patch.object(StockMovement.objects, "create", fail_second):
            with self.assertRaises(RuntimeError):
                receive_purchase(purchase_id=purchase.pk, user=self.owner)

        purchase.refresh_from_db()
        self.assertEqual(purchase.status, SupplierPurchase.Status.DRAFT)
        self.assertIsNone(purchase.received_at)
        self.assertEqual(StockMovement.objects.count(), 0)


class PurchaseAPITests(PurchaseTestBase):
    def url(self, name, business=None, pk=None):
        kwargs = {"business_id": (business or self.business).pk}
        if pk:
            kwargs["pk"] = pk
        return reverse(f"purchases:{name}", kwargs=kwargs)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def payload(self, **over):
        data = {
            "supplier_id": str(self.supplier.pk),
            "purchase_number": "API-1",
            "purchased_at": timezone.now().isoformat(),
            "lines": [{
                "product_id": str(self.tracked.pk),
                "quantity": "4.000",
                "unit_price": "25.0000",
            }],
        }
        data.update(over)
        return data

    def create(self, **over):
        res = self.client_for(self.owner).post(
            self.url("purchase-list-create"), self.payload(**over), format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        return res.data

    def test_full_lifecycle(self):
        client = self.client_for(self.manager)
        created = self.create()
        self.assertEqual(created["status"], "DRAFT")
        self.assertEqual(created["total_amount"], "100.00")
        pk = created["id"]

        res = client.patch(
            self.url("purchase-detail", pk=pk), {"notes": "edited"}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["notes"], "edited")

        res = client.post(self.url("purchase-receive", pk=pk))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "RECEIVED")
        self.assertEqual(
            StockMovement.objects.filter(purchase_line__purchase_id=pk).count(), 1
        )

        for name in ("purchase-receive", "purchase-cancel"):
            self.assertEqual(client.post(self.url(name, pk=pk)).status_code, 400)
        res = client.patch(
            self.url("purchase-detail", pk=pk), {"notes": "x"}, format="json"
        )
        self.assertEqual(res.status_code, 400)

    def test_cancel_draft_via_api(self):
        pk = self.create()["id"]
        client = self.client_for(self.owner)
        res = client.post(
            self.url("purchase-cancel", pk=pk), {"reason": "dup"}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "CANCELLED")
        self.assertEqual(res.data["cancel_reason"], "dup")
        self.assertEqual(client.post(self.url("purchase-receive", pk=pk)).status_code, 400)

    def test_unauthenticated_rejected(self):
        client = APIClient()
        self.assertIn(client.get(self.url("purchase-list-create")).status_code, (401, 403))
        self.assertIn(client.post(self.url("purchase-list-create"), {}).status_code, (401, 403))

    def test_staff_denied_everything(self):
        pk = self.create()["id"]
        client = self.client_for(self.staff)
        self.assertEqual(client.get(self.url("purchase-list-create")).status_code, 403)
        self.assertEqual(
            client.post(self.url("purchase-list-create"), self.payload(purchase_number="S"),
                        format="json").status_code, 403)
        self.assertEqual(client.get(self.url("purchase-detail", pk=pk)).status_code, 403)
        self.assertEqual(client.post(self.url("purchase-receive", pk=pk)).status_code, 403)
        self.assertEqual(client.post(self.url("purchase-cancel", pk=pk)).status_code, 403)
        self.assertEqual(SupplierPurchase.objects.get(pk=pk).status, "DRAFT")

    def test_cross_tenant_access_denied_or_hidden(self):
        pk = self.create()["id"]
        outsider = self.client_for(self.outsider)

        # Outsider is not a member of business A.
        self.assertEqual(outsider.get(self.url("purchase-list-create")).status_code, 403)
        self.assertEqual(outsider.get(self.url("purchase-detail", pk=pk)).status_code, 403)

        # Using their own business URL with A's purchase id: plain 404.
        for call in (
            lambda: outsider.get(self.url("purchase-detail", self.other_business, pk)),
            lambda: outsider.post(self.url("purchase-receive", self.other_business, pk)),
            lambda: outsider.post(self.url("purchase-cancel", self.other_business, pk), {"reason": "x"}, format="json"),
            lambda: outsider.patch(self.url("purchase-detail", self.other_business, pk),
                                   {"notes": "x"}, format="json"),
        ):
            self.assertEqual(call().status_code, 404)
        self.assertEqual(SupplierPurchase.objects.get(pk=pk).status, "DRAFT")

        # Their list never includes A's purchases.
        res = outsider.get(self.url("purchase-list-create", self.other_business))
        self.assertEqual(res.data["count"], 0)

    def test_foreign_ids_rejected_without_leaking(self):
        client = self.client_for(self.owner)
        for over in (
            {"supplier_id": str(self.foreign_supplier.pk)},
            {"lines": [{"product_id": str(self.foreign_product.pk),
                        "quantity": "1", "unit_price": "1"}]},
        ):
            res = client.post(
                self.url("purchase-list-create"), self.payload(**over), format="json"
            )
            self.assertEqual(res.status_code, 404)
            self.assertNotIn("Foreign", res.content.decode())
        self.assertEqual(SupplierPurchase.objects.count(), 0)

    def test_validation_errors_are_400(self):
        client = self.client_for(self.owner)
        bad = [
            self.payload(lines=[]),
            self.payload(lines=[{"product_id": str(self.tracked.pk),
                                 "quantity": "0", "unit_price": "1"}]),
            self.payload(lines=[{"product_id": str(self.tracked.pk),
                                 "quantity": "1", "unit_price": "-1"}]),
            self.payload(purchased_at="2026-01-01T10:00:00"),  # naive
        ]
        for data in bad:
            res = client.post(self.url("purchase-list-create"), data, format="json")
            self.assertEqual(res.status_code, 400, data)

    def test_duplicate_number_is_400(self):
        self.create()
        res = self.client_for(self.owner).post(
            self.url("purchase-list-create"), self.payload(), format="json"
        )
        self.assertEqual(res.status_code, 400)

    def test_list_filters_and_pagination(self):
        a = self.create(purchase_number="AAA-1")
        b = self.create(purchase_number="BBB-2", supplier_id=str(self.supplier2.pk))
        client = self.client_for(self.owner)
        client.post(self.url("purchase-receive", pk=b["id"]))
        url = self.url("purchase-list-create")

        self.assertEqual(client.get(url).data["count"], 2)
        self.assertEqual(client.get(url, {"status": "RECEIVED"}).data["count"], 1)
        self.assertEqual(client.get(url, {"supplier": str(self.supplier.pk)}).data["count"], 1)
        self.assertEqual(client.get(url, {"purchase_number": "bbb"}).data["results"][0]["id"], b["id"])
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(client.get(url, {"date_from": tomorrow}).data["count"], 0)
        self.assertEqual(client.get(url, {"page_size": 1}).data["results"].__len__(), 1)
        self.assertEqual(client.get(url, {"date_from": tomorrow, "date_to": "2000-01-01"}).status_code, 400)
        self.assertEqual(client.get(url, {"status": "NOPE"}).status_code, 400)
        self.assertEqual(a["status"], "DRAFT")

    def test_list_query_count_is_constant(self):
        for i in range(5):
            self.create(purchase_number=f"Q-{i}")
        client = self.client_for(self.owner)
        with self.assertNumQueries(5):
            client.get(self.url("purchase-list-create"))


class SupplierPaymentIntegrationTests(PurchaseTestBase):
    def pay(self, number, amount, method="CASH", allocations=None, supplier=None):
        return record_supplier_payment(
            business_id=self.business.pk,
            supplier_id=(supplier or self.supplier).pk,
            payment_number=number,
            paid_at=timezone.now(),
            amount=amount,
            payment_method=method,
            allocations=allocations,
            user=self.owner,
        )

    def balance(self):
        return get_supplier_balance(
            business_id=self.business.pk, supplier_id=self.supplier.pk
        )

    def test_cash_and_non_cash_methods(self):
        cash = self.pay("PAY-1", "10.00", "CASH")
        upi = self.pay("PAY-2", "10.00", "UPI")
        self.assertEqual(cash.payment_method, "CASH")
        self.assertEqual(upi.payment_method, "UPI")
        with self.assertRaises(ValidationError):
            self.pay("PAY-3", "10.00", "BITCOIN")

    def test_unallocated_advance_then_allocated_to_received_purchase(self):
        payment = self.pay("ADV-1", "300.00", "UPI")
        self.assertEqual(self.balance()["available_credit"], Decimal("300.00"))
        self.assertFalse(SupplierPaymentAllocation.objects.exists())

        purchase = self.received()  # 500.00
        allocate_supplier_advance(
            payment_id=payment.pk, purchase_id=purchase.pk,
            amount="300.00", user=self.owner,
        )
        bal = self.balance()
        self.assertEqual(bal["outstanding_amount"], Decimal("200.00"))
        self.assertEqual(bal["available_credit"], Decimal("0.00"))

    def test_partial_and_multiple_allocations(self):
        p1 = self.received("PUR-1")
        p2 = self.received("PUR-2")  # 500 each
        payment = self.pay(
            "PAY-A", "600.00",
            allocations=[
                {"purchase_id": p1.pk, "amount": "500.00"},
                {"purchase_id": p2.pk, "amount": "50.00"},
            ],
        )
        self.assertEqual(payment.allocations.count(), 2)
        bal = self.balance()
        self.assertEqual(bal["allocated_payments"], Decimal("550.00"))
        self.assertEqual(bal["outstanding_amount"], Decimal("450.00"))
        self.assertEqual(bal["available_credit"], Decimal("50.00"))

    def test_over_allocation_rejected(self):
        purchase = self.received()
        payment = self.pay("PAY-B", "100.00")
        with self.assertRaises(ValidationError):
            allocate_supplier_advance(
                payment_id=payment.pk, purchase_id=purchase.pk,
                amount="100.01", user=self.owner,
            )
        with self.assertRaises(ValidationError):
            self.pay("PAY-C", "100.00",
                     allocations=[{"purchase_id": purchase.pk, "amount": "100.01"}])
        with self.assertRaises(ValidationError):
            self.pay("PAY-D", "1000.00",
                     allocations=[{"purchase_id": purchase.pk, "amount": "500.01"}])
        self.assertFalse(SupplierPaymentAllocation.objects.exists())

    def test_allocation_to_draft_other_supplier_or_business_rejected(self):
        payment = self.pay("PAY-E", "100.00")
        draft = self.draft("PUR-D")
        other_supplier_purchase = self.received("PUR-S2", supplier=self.supplier2)
        foreign = create_purchase(
            business_id=self.other_business.pk,
            supplier_id=self.foreign_supplier.pk,
            purchase_number="F-1", purchased_at=timezone.now(),
            lines=[{"product_id": self.foreign_product.pk,
                    "quantity": "1", "unit_price": "10"}],
            user=self.outsider,
        )
        receive_purchase(purchase_id=foreign.pk, user=self.outsider)
        for target in (draft, other_supplier_purchase, foreign):
            with self.assertRaises(ValidationError):
                allocate_supplier_advance(
                    payment_id=payment.pk, purchase_id=target.pk,
                    amount="10.00", user=self.owner,
                )

    def test_cancelled_draft_never_affects_balance_or_statement(self):
        purchase = self.draft()
        cancel_draft_purchase(purchase_id=purchase.pk, user=self.owner, reason="test")
        self.assertEqual(self.balance()["total_purchases"], Decimal("0.00"))
        stmt = get_supplier_account_statement(
            business_id=self.business.pk, supplier_id=self.supplier.pk
        )
        self.assertEqual(stmt["transactions"], [])

    def test_reversal_restores_outstanding_and_keeps_history(self):
        purchase = self.received()
        payment = self.pay(
            "PAY-R", "500.00",
            allocations=[{"purchase_id": purchase.pk, "amount": "500.00"}],
        )
        self.assertEqual(self.balance()["outstanding_amount"], Decimal("0.00"))

        reverse_supplier_payment(payment_id=payment.pk, reason="bounced", user=self.owner)
        bal = self.balance()
        self.assertEqual(bal["outstanding_amount"], Decimal("500.00"))
        self.assertEqual(bal["available_credit"], Decimal("0.00"))
        self.assertTrue(SupplierPayment.objects.filter(pk=payment.pk).exists())
        self.assertEqual(payment.allocations.count(), 1)

        with self.assertRaises(ValidationError):
            reverse_supplier_payment(payment_id=payment.pk, reason="again", user=self.owner)

        stmt = get_supplier_account_statement(
            business_id=self.business.pk, supplier_id=self.supplier.pk
        )
        self.assertEqual(
            [t["type"] for t in stmt["transactions"]],
            ["PURCHASE", "PAYMENT", "PAYMENT_REVERSAL"],
        )
        self.assertEqual(stmt["closing_balance"], Decimal("500.00"))

    def test_statement_matches_selector(self):
        self.received()
        self.pay("PAY-S", "200.00")
        stmt = get_supplier_account_statement(
            business_id=self.business.pk, supplier_id=self.supplier.pk
        )
        self.assertEqual(stmt["closing_balance"], Decimal("300.00"))
        self.assertEqual(stmt["total_purchases"], self.balance()["total_purchases"])
