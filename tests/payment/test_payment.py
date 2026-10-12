
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.businesses.models import Business, BusinessMembership
from apps.suppliers.models import Supplier
from apps.purchases.models import SupplierPurchase
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
)
from apps.payments.services import record_supplier_payment
from apps.payments.selectors import get_supplier_balance


User = get_user_model()


class SupplierPaymentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="payment_owner",
            password="test-password",
        )

        self.business = Business.objects.create(
            name="Test Dairy",
            slug="test-dairy",
        )

        BusinessMembership.objects.create(
            business=self.business,
            user=self.user,
            role=BusinessMembership.Role.OWNER,
            is_active=True,
        )

        self.supplier = Supplier.objects.create(
            business=self.business,
            supplier_code="SUP001",
            name="Test Supplier",
        )

        self.purchase = self.create_purchase(
            purchase_number="PUR001",
            total_amount="1000.00",
        )

    def create_purchase(
        self,
        purchase_number,
        total_amount,
        supplier=None,
        business=None,
        status=SupplierPurchase.Status.RECEIVED,
    ):
        return SupplierPurchase.objects.create(
            business=business or self.business,
            supplier=supplier or self.supplier,
            purchase_number=purchase_number,
            purchased_at=timezone.now(),
            status=status,
            total_amount=Decimal(total_amount),
            created_by=self.user,
            received_at=timezone.now()
            if status == SupplierPurchase.Status.RECEIVED
            else None,
        )

    def record_payment(
        self,
        *,
        payment_number="PAY001",
        amount="500.00",
        allocations=None,
        supplier_id=None,
        business_id=None,
    ):
        return record_supplier_payment(
            business_id=business_id or self.business.id,
            supplier_id=supplier_id or self.supplier.id,
            payment_number=payment_number,
            paid_at=timezone.now(),
            amount=amount,
            user=self.user,
            payment_method=SupplierPayment.PaymentMethod.CASH,
            allocations=allocations or [],
        )

    def test_record_payment_successfully(self):
        payment = self.record_payment(amount="500.00")

        self.assertEqual(payment.amount, Decimal("500.00"))
        self.assertEqual(payment.business, self.business)
        self.assertEqual(payment.supplier, self.supplier)
        self.assertEqual(
            SupplierPayment.objects.filter(pk=payment.pk).count(),
            1,
        )

    def test_partial_payment_updates_outstanding_balance(self):
        self.record_payment(
            payment_number="PAY001",
            amount="400.00",
            allocations=[
                {
                    "purchase_id": self.purchase.id,
                    "amount": "400.00",
                }
            ],
        )

        balance = get_supplier_balance(
            business_id=self.business.id,
            supplier_id=self.supplier.id,
        )

        self.assertEqual(
            balance["total_purchases"], Decimal("1000.00")
        )
        self.assertEqual(
            balance["allocated_payments"], Decimal("400.00")
        )
        self.assertEqual(
            balance["outstanding_amount"], Decimal("600.00")
        )

    def test_full_payment_clears_outstanding_balance(self):
        self.record_payment(
            amount="1000.00",
            allocations=[
                {
                    "purchase_id": self.purchase.id,
                    "amount": "1000.00",
                }
            ],
        )

        balance = get_supplier_balance(
            business_id=self.business.id,
            supplier_id=self.supplier.id,
        )

        self.assertEqual(
            balance["outstanding_amount"], Decimal("0.00")
        )

    def test_advance_payment_without_allocation(self):
        payment = self.record_payment(amount="700.00")

        self.assertEqual(payment.allocations.count(), 0)

        balance = get_supplier_balance(
            business_id=self.business.id,
            supplier_id=self.supplier.id,
        )

        # An unallocated advance does not reduce purchase outstanding.
        self.assertEqual(
            balance["outstanding_amount"], Decimal("1000.00")
        )

    def test_allocation_cannot_exceed_payment_amount(self):
        with self.assertRaises(ValidationError):
            self.record_payment(
                amount="300.00",
                allocations=[
                    {
                        "purchase_id": self.purchase.id,
                        "amount": "400.00",
                    }
                ],
            )

        self.assertEqual(SupplierPayment.objects.count(), 0)

    def test_allocation_cannot_exceed_purchase_outstanding(self):
        with self.assertRaises(ValidationError):
            self.record_payment(
                amount="1200.00",
                allocations=[
                    {
                        "purchase_id": self.purchase.id,
                        "amount": "1200.00",
                    }
                ],
            )

        self.assertEqual(SupplierPayment.objects.count(), 0)

    def test_cannot_allocate_to_another_supplier_purchase(self):
        another_supplier = Supplier.objects.create(
            business=self.business,
            supplier_code="SUP002",
            name="Another Supplier",
        )

        other_purchase = self.create_purchase(
            purchase_number="PUR002",
            total_amount="500.00",
            supplier=another_supplier,
        )

        with self.assertRaises(ValidationError):
            self.record_payment(
                amount="200.00",
                allocations=[
                    {
                        "purchase_id": other_purchase.id,
                        "amount": "200.00",
                    }
                ],
            )

        self.assertEqual(SupplierPayment.objects.count(), 0)

    def test_cannot_allocate_to_draft_purchase(self):
        draft_purchase = self.create_purchase(
            purchase_number="PUR003",
            total_amount="500.00",
            status=SupplierPurchase.Status.DRAFT,
        )

        with self.assertRaises(ValidationError):
            self.record_payment(
                amount="200.00",
                allocations=[
                    {
                        "purchase_id": draft_purchase.id,
                        "amount": "200.00",
                    }
                ],
            )

        self.assertEqual(SupplierPayment.objects.count(), 0)

    def test_payment_amount_must_be_positive(self):
        with self.assertRaises(ValidationError):
            self.record_payment(amount="0.00")

        self.assertEqual(SupplierPayment.objects.count(), 0)

    def test_payment_requires_business_membership(self):
        outsider = User.objects.create_user(
            username="outsider",
            password="test-password",
        )

        with self.assertRaises(Exception):
            record_supplier_payment(
                business_id=self.business.id,
                supplier_id=self.supplier.id,
                payment_number="PAY-OUTSIDER",
                paid_at=timezone.now(),
                amount="100.00",
                user=outsider,
            )

        self.assertEqual(SupplierPayment.objects.count(), 0)

    def test_payment_and_allocation_cannot_be_deleted(self):
        payment = self.record_payment(
            amount="200.00",
            allocations=[
                {
                    "purchase_id": self.purchase.id,
                    "amount": "200.00",
                }
            ],
        )

        allocation = SupplierPaymentAllocation.objects.get(
            payment=payment
        )

        with self.assertRaises(ValueError):
            payment.delete()

        with self.assertRaises(ValueError):
            allocation.delete()
