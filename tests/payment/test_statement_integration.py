
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.businesses.models import Business, BusinessMembership
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)
from apps.payments.statements import get_supplier_account_statement
from apps.purchases.models import SupplierPurchase
from apps.suppliers.models import Supplier
from apps.payments.services import (
    allocate_supplier_advance,
    record_supplier_payment,
)

class SupplierStatementRegressionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="statement_test_owner",
            password="test-password",
        )

        self.business = Business.objects.create(
            name="Statement Dairy",
            slug="statement-dairy",
        )

        BusinessMembership.objects.create(
            business=self.business,
            user=self.user,
            role=BusinessMembership.Role.OWNER,
            is_active=True,
        )

        self.supplier = Supplier.objects.create(
            business=self.business,
            supplier_code="SUP-001",
            name="Test Supplier",
        )

        self.now = timezone.now()

        self.purchase = SupplierPurchase.objects.create(
            business=self.business,
            supplier=self.supplier,
            purchase_number="PUR-001",
            purchased_at=self.now,
            status=SupplierPurchase.Status.RECEIVED,
            total_amount=Decimal("100.00"),
            created_by=self.user,
            received_at=self.now,
        )

        self.payment = SupplierPayment.objects.create(
            business=self.business,
            supplier=self.supplier,
            payment_number="PAY-001",
            paid_at=self.now,
            amount=Decimal("100.00"),
            created_by=self.user,
        )

        SupplierPaymentAllocation.objects.create(
            business=self.business,
            payment=self.payment,
            purchase=self.purchase,
            amount=Decimal("100.00"),
        )

    def get_statement(self):
        return get_supplier_account_statement(
            business_id=self.business.pk,
            supplier_id=self.supplier.pk,
            end_date=timezone.localdate(),
        )

    def test_active_payment_allocation_is_counted(self):
        statement = self.get_statement()

        self.assertEqual(
            statement["allocated_payments"],
            Decimal("100.00"),
        )
        self.assertEqual(
            statement["outstanding_amount"],
            Decimal("0.00"),
        )

    def test_reversed_payment_is_excluded(self):
        SupplierPaymentReversal.objects.create(
            business=self.business,
            payment=self.payment,
            amount=self.payment.amount,
            reason="Regression test",
            reversed_at=self.now,
            created_by=self.user,
        )

        statement = self.get_statement()

        self.assertEqual(
            statement["total_payments"],
            Decimal("0.00"),
        )
        self.assertEqual(
            statement["allocated_payments"],
            Decimal("0.00"),
        )
        self.assertEqual(
            statement["outstanding_amount"],
            Decimal("100.00"),
        )

    def test_cross_business_supplier_payment_is_rejected(self):
        other_business = Business.objects.create(
            name="Other Dairy",
            slug="other-dairy",
        )

        other_supplier = Supplier.objects.create(
            business=other_business,
            supplier_code="SUP-001",
            name="Other Supplier",
        )

        with self.assertRaises(ValidationError):
            record_supplier_payment(
                business_id=self.business.pk,
                supplier_id=other_supplier.pk,
                payment_number="PAY-CROSS-001",
                paid_at=self.now,
                amount=Decimal("10.00"),
                user=self.user,
            )

        self.assertFalse(
            SupplierPayment.objects.filter(
                payment_number="PAY-CROSS-001",
            ).exists()
        )

    def test_reversed_allocation_does_not_block_replacement_payment(self):
        SupplierPaymentReversal.objects.create(
            business=self.business,
            payment=self.payment,
            amount=self.payment.amount,
            reason="Replacement payment regression",
            reversed_at=self.now,
            created_by=self.user,
        )

        replacement = record_supplier_payment(
            business_id=self.business.pk,
            supplier_id=self.supplier.pk,
            payment_number="PAY-REPLACEMENT-001",
            paid_at=self.now,
            amount=Decimal("100.00"),
            user=self.user,
            allocations=[
                {
                    "purchase_id": self.purchase.pk,
                    "amount": Decimal("100.00"),
                }
            ],
        )

        allocation = SupplierPaymentAllocation.objects.get(
            payment=replacement,
            purchase=self.purchase,
        )
        self.assertEqual(allocation.amount, Decimal("100.00"))


    def test_reversed_allocation_does_not_block_advance_allocation(self):
        SupplierPaymentReversal.objects.create(
            business=self.business,
            payment=self.payment,
            amount=self.payment.amount,
            reason="Replacement advance regression",
            reversed_at=self.now,
            created_by=self.user,
        )

        advance = record_supplier_payment(
            business_id=self.business.pk,
            supplier_id=self.supplier.pk,
            payment_number="PAY-ADVANCE-REPLACEMENT-001",
            paid_at=self.now,
            amount=Decimal("100.00"),
            user=self.user,
        )

        allocation = allocate_supplier_advance(
            payment_id=advance.pk,
            purchase_id=self.purchase.pk,
            amount=Decimal("100.00"),
            user=self.user,
        )

        self.assertEqual(allocation.amount, Decimal("100.00"))
