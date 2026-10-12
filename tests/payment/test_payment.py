
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from django.db import IntegrityError, transaction
from django.db.models import Sum
from apps.businesses.models import Business, BusinessMembership
from apps.suppliers.models import Supplier
from apps.purchases.models import SupplierPurchase
from apps.payments.models import (
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)
from apps.payments.services import (
    record_supplier_payment,
    allocate_supplier_advance,
    reverse_supplier_payment,
)
from apps.payments.selectors import get_supplier_balance

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import close_old_connections
from django.test import TransactionTestCase

User = get_user_model()


class SupplierPaymentConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        User = get_user_model()

        self.user = User.objects.create_user(
            username="concurrency_owner",
            password="test-password",
        )

        self.business = Business.objects.create(
            name="Concurrency Dairy",
            slug="concurrency-dairy",
        )

        BusinessMembership.objects.create(
            business=self.business,
            user=self.user,
            role=BusinessMembership.Role.OWNER,
            is_active=True,
        )

        self.supplier = Supplier.objects.create(
            business=self.business,
            supplier_code="CONC-SUP-001",
            name="Concurrency Supplier",
        )

        self.purchase = SupplierPurchase.objects.create(
            business=self.business,
            supplier=self.supplier,
            purchase_number="CONC-PUR-001",
            purchased_at=timezone.now(),
            status=SupplierPurchase.Status.RECEIVED,
            total_amount=Decimal("1000.00"),
            created_by=self.user,
            received_at=timezone.now(),
        )

    def test_concurrent_allocations_cannot_overpay_purchase(self):
        barrier = Barrier(2)

        def attempt_payment(payment_number):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)

                # Start both requests together.
                barrier.wait(timeout=10)

                record_supplier_payment(
                    business_id=self.business.id,
                    supplier_id=self.supplier.id,
                    payment_number=payment_number,
                    paid_at=timezone.now(),
                    amount=Decimal("700.00"),
                    user=user,
                    allocations=[
                        {
                            "purchase_id": self.purchase.id,
                            "amount": Decimal("700.00"),
                        }
                    ],
                )
                return "success"

            except ValidationError:
                return "rejected"

            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    attempt_payment,
                    ["CONC-PAY-001", "CONC-PAY-002"],
                )
            )

        self.assertEqual(results.count("success"), 1)
        self.assertEqual(results.count("rejected"), 1)

        allocated = (
            SupplierPaymentAllocation.objects.filter(
                purchase=self.purchase
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )

        self.assertEqual(allocated, Decimal("700.00"))


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


    def test_duplicate_payment_number_is_rejected(self):
        self.record_payment(
            payment_number="PAY-DUP-001",
            amount="100.00",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.record_payment(
                    payment_number="PAY-DUP-001",
                    amount="200.00",
                )

        self.assertEqual(
            SupplierPayment.objects.filter(
                business=self.business,
                payment_number="PAY-DUP-001",
            ).count(),
            1,
        )

    def test_one_payment_can_cover_multiple_purchases(self):
        second_purchase = self.create_purchase(
            purchase_number="PUR-MULTI-002",
            total_amount="500.00",
        )

        payment = self.record_payment(
            payment_number="PAY-MULTI-001",
            amount="1200.00",
            allocations=[
                {
                    "purchase_id": self.purchase.id,
                    "amount": "700.00",
                },
                {
                    "purchase_id": second_purchase.id,
                    "amount": "500.00",
                },
            ],
        )

        self.assertEqual(payment.allocations.count(), 2)

        balance = get_supplier_balance(
            business_id=self.business.id,
            supplier_id=self.supplier.id,
        )

        self.assertEqual(
            balance["total_purchases"],
            Decimal("1500.00"),
        )
        self.assertEqual(
            balance["allocated_payments"],
            Decimal("1200.00"),
        )
        self.assertEqual(
            balance["outstanding_amount"],
            Decimal("300.00"),
        )

    def test_payment_cannot_allocate_to_another_business_purchase(self):
        other_business = Business.objects.create(
            name="Other Dairy",
            slug="other-dairy",
        )

        BusinessMembership.objects.create(
            business=other_business,
            user=self.user,
            role=BusinessMembership.Role.OWNER,
            is_active=True,
        )

        other_supplier = Supplier.objects.create(
            business=other_business,
            supplier_code="OTHER-SUP-001",
            name="Other Business Supplier",
        )

        with self.assertRaises(ValidationError):
            self.record_payment(
                payment_number="PAY-CROSS-BUSINESS-001",
                amount="100.00",
                business_id=other_business.id,
                supplier_id=other_supplier.id,
                allocations=[
                    {
                        "purchase_id": self.purchase.id,
                        "amount": "100.00",
                    }
                ],
            )

        self.assertFalse(
            SupplierPayment.objects.filter(
                payment_number="PAY-CROSS-BUSINESS-001",
            ).exists()
        )

    def test_queryset_update_is_blocked(self):
        payment = self.record_payment(
            payment_number="PAY-IMM-001",
            amount="100.00",
        )

        with self.assertRaises(ValueError):
            SupplierPayment.objects.filter(
                pk=payment.pk
            ).update(notes="Tampered")

        payment.refresh_from_db()
        self.assertEqual(payment.notes, "")

    def test_queryset_delete_is_blocked(self):
        payment = self.record_payment(
            payment_number="PAY-IMM-002",
            amount="100.00",
        )

        with self.assertRaises(ValueError):
            SupplierPayment.objects.filter(
                pk=payment.pk
            ).delete()

        self.assertTrue(
            SupplierPayment.objects.filter(pk=payment.pk).exists()
        )

    def test_allocation_queryset_update_is_blocked(self):
        payment = self.record_payment(
            payment_number="PAY-IMM-003",
            amount="200.00",
            allocations=[
                {
                    "purchase_id": self.purchase.id,
                    "amount": "100.00",
                }
            ],
        )

        allocation = SupplierPaymentAllocation.objects.get(
            payment=payment
        )

        with self.assertRaises(ValueError):
            SupplierPaymentAllocation.objects.filter(
                pk=allocation.pk
            ).update(amount=Decimal("50.00"))

        allocation.refresh_from_db()
        self.assertEqual(allocation.amount, Decimal("100.00"))

    def test_allocation_queryset_delete_is_blocked(self):
        payment = self.record_payment(
            payment_number="PAY-IMM-004",
            amount="200.00",
            allocations=[
                {
                    "purchase_id": self.purchase.id,
                    "amount": "100.00",
                }
            ],
        )

        allocation = SupplierPaymentAllocation.objects.get(
            payment=payment
        )

        with self.assertRaises(ValueError):
            SupplierPaymentAllocation.objects.filter(
                pk=allocation.pk
            ).delete()

        self.assertTrue(
            SupplierPaymentAllocation.objects.filter(
                pk=allocation.pk
            ).exists()
        )
        
    def test_allocate_existing_advance_to_purchase(self):
        payment = self.record_payment(
            payment_number="PAY-ADV-001",
            amount="700.00",
        )

        allocation = allocate_supplier_advance(
            payment_id=payment.id,
            purchase_id=self.purchase.id,
            amount="500.00",
            user=self.user,
        )

        self.assertEqual(allocation.amount, Decimal("500.00"))
        self.assertEqual(
            SupplierPaymentAllocation.objects.filter(
                payment=payment
            ).aggregate(total=__import__(
                "django.db.models", fromlist=["Sum"]
            ).Sum("amount"))["total"],
            Decimal("500.00"),
        )

        balance = get_supplier_balance(
            business_id=self.business.id,
            supplier_id=self.supplier.id,
        )
        self.assertEqual(
            balance["outstanding_amount"], Decimal("500.00")
        )

    def test_cannot_allocate_more_than_remaining_advance(self):
        payment = self.record_payment(
            payment_number="PAY-ADV-002",
            amount="700.00",
        )

        allocate_supplier_advance(
            payment_id=payment.id,
            purchase_id=self.purchase.id,
            amount="500.00",
            user=self.user,
        )

        second_purchase = self.create_purchase(
            purchase_number="PUR-ADV-002",
            total_amount="1000.00",
        )

        with self.assertRaises(ValidationError):
            allocate_supplier_advance(
                payment_id=payment.id,
                purchase_id=second_purchase.id,
                amount="300.00",
                user=self.user,
            )

        self.assertEqual(payment.allocations.count(), 1)

    def test_cannot_allocate_more_than_purchase_outstanding(self):
        payment = self.record_payment(
            payment_number="PAY-ADV-003",
            amount="1200.00",
        )

        with self.assertRaises(ValidationError):
            allocate_supplier_advance(
                payment_id=payment.id,
                purchase_id=self.purchase.id,
                amount="1200.00",
                user=self.user,
            )

        self.assertEqual(payment.allocations.count(), 0)

    def test_advance_cannot_be_allocated_to_another_supplier(self):
        payment = self.record_payment(
            payment_number="PAY-ADV-004",
            amount="500.00",
        )

        other_supplier = Supplier.objects.create(
            business=self.business,
            supplier_code="SUP-ADV-OTHER",
            name="Other Supplier",
        )

        other_purchase = self.create_purchase(
            purchase_number="PUR-ADV-OTHER",
            total_amount="500.00",
            supplier=other_supplier,
        )

        with self.assertRaises(ValidationError):
            allocate_supplier_advance(
                payment_id=payment.id,
                purchase_id=other_purchase.id,
                amount="200.00",
                user=self.user,
            )

        self.assertEqual(payment.allocations.count(), 0)
        

    def test_reversal_restores_purchase_outstanding_balance(self):
        payment = self.record_payment(
            payment_number="PAY-REV-001",
            amount="400.00",
            allocations=[
                {
                    "purchase_id": self.purchase.id,
                    "amount": "400.00",
                }
            ],
        )

        reversal = reverse_supplier_payment(
            payment_id=payment.id,
            reason="Payment entered incorrectly",
            user=self.user,
        )

        self.assertEqual(reversal.amount, Decimal("400.00"))

        balance = get_supplier_balance(
            business_id=self.business.id,
            supplier_id=self.supplier.id,
        )
        self.assertEqual(
            balance["outstanding_amount"], Decimal("1000.00")
        )

        # Original payment and allocation remain in the database.
        self.assertTrue(
            SupplierPayment.objects.filter(pk=payment.pk).exists()
        )
        self.assertEqual(payment.allocations.count(), 1)

    def test_payment_cannot_be_reversed_twice(self):
        payment = self.record_payment(
            payment_number="PAY-REV-002",
            amount="100.00",
        )

        reverse_supplier_payment(
            payment_id=payment.id,
            reason="Incorrect entry",
            user=self.user,
        )

        with self.assertRaises(ValidationError):
            reverse_supplier_payment(
                payment_id=payment.id,
                reason="Second reversal attempt",
                user=self.user,
            )

        self.assertEqual(
            SupplierPaymentReversal.objects.filter(
                payment=payment
            ).count(),
            1,
        )

    def test_reversal_requires_reason(self):
        payment = self.record_payment(
            payment_number="PAY-REV-003",
            amount="100.00",
        )

        with self.assertRaises(ValidationError):
            reverse_supplier_payment(
                payment_id=payment.id,
                reason="   ",
                user=self.user,
            )

        self.assertFalse(
            SupplierPaymentReversal.objects.filter(
                payment=payment
            ).exists()
        )

    def test_reversed_payment_cannot_be_allocated_later(self):
        payment = self.record_payment(
            payment_number="PAY-REV-004",
            amount="500.00",
        )

        reverse_supplier_payment(
            payment_id=payment.id,
            reason="Wrong payment",
            user=self.user,
        )

        with self.assertRaises(ValidationError):
            allocate_supplier_advance(
                payment_id=payment.id,
                purchase_id=self.purchase.id,
                amount="100.00",
                user=self.user,
            )

        self.assertEqual(payment.allocations.count(), 0)
