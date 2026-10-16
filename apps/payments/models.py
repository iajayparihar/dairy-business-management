
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from apps.businesses.models import Business
from apps.purchases.models import SupplierPurchase
from apps.suppliers.models import Supplier


class ImmutableLedgerQuerySet(models.QuerySet):
    """Prevent bulk ORM operations from bypassing ledger safeguards."""

    def update(self, **kwargs):
        raise ValueError(
            "Posted financial records cannot be bulk-updated."
        )

    def delete(self):
        raise ValueError(
            "Posted financial records cannot be bulk-deleted."
        )

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError(
            "Posted financial records cannot be bulk-updated."
        )

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        raise ValueError(
            "Use the payment service to create financial records."
        )


ImmutableLedgerManager = models.Manager.from_queryset(
    ImmutableLedgerQuerySet
)


class SupplierPayment(models.Model):
    class PaymentMethod(models.TextChoices):
        CASH = "CASH", "Cash"
        UPI = "UPI", "UPI"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"
        CHEQUE = "CHEQUE", "Cheque"
        OTHER = "OTHER", "Other"
        
    objects = ImmutableLedgerManager()
    
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="supplier_payments",
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    payment_number = models.CharField(max_length=50)
    paid_at = models.DateTimeField()
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
    )
    reference_number = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )
    notes = models.TextField(blank=True, default="")
    # Client-generated key making retried submissions safe.
    client_ref = models.CharField(max_length=64, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments_supplier_payment"
        ordering = ["-paid_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "payment_number"],
                name="uniq_supplier_payment_number",
            ),
            models.UniqueConstraint(
                fields=["business", "client_ref"],
                condition=Q(client_ref__isnull=False),
                name="uniq_supplier_payment_client_ref",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="supplier_payment_amount_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "supplier", "paid_at"],
                name="supp_payment_biz_date_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError(
                "Posted supplier payments are immutable."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            "Supplier payments cannot be deleted. "
            "Use a reversal or adjustment."
        )

    def __str__(self):
        return f"{self.payment_number} - {self.amount}"


class SupplierPaymentAllocation(models.Model):
    """
    Applies part of a supplier payment to a received purchase.

    Any unallocated payment amount remains an advance/credit.
    """
    
    objects = ImmutableLedgerManager()
    
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="supplier_payment_allocations",
    )
    payment = models.ForeignKey(
        SupplierPayment,
        on_delete=models.PROTECT,
        related_name="allocations",
    )
    purchase = models.ForeignKey(
        SupplierPurchase,
        on_delete=models.PROTECT,
        related_name="payment_allocations",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments_supplier_payment_allocation"
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "purchase"],
                name="uniq_supplier_payment_purchase",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="supplier_allocation_amount_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "purchase"],
                name="supp_alloc_biz_purchase_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError(
                "Payment allocations are immutable."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            "Payment allocations cannot be deleted. "
            "Use a reversal or adjustment."
        )

    def __str__(self):
        return f"{self.payment.payment_number} - {self.amount}"




class SupplierPaymentReversal(models.Model):
    """
    One (full or partial) reversal of a posted supplier payment.

    A payment can have several reversals; together they never exceed the
    payment amount (enforced by the service under the payment row lock).
    The original payment and its allocations are never edited: the part of
    a reversal that had already been allocated is recorded explicitly as
    SupplierPaymentAllocationReversal rows. The rest came out of the
    unallocated advance.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="supplier_payment_reversals",
    )
    payment = models.ForeignKey(
        SupplierPayment,
        on_delete=models.PROTECT,
        related_name="reversals",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    reason = models.TextField()
    reversed_at = models.DateTimeField()
    # Client-generated key making retried submissions safe.
    client_ref = models.CharField(max_length=64, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableLedgerManager()

    class Meta:
        db_table = "payments_supplier_payment_reversal"
        ordering = ["-reversed_at", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="supplier_payment_reversal_positive",
            ),
            models.UniqueConstraint(
                fields=["business", "client_ref"],
                condition=Q(client_ref__isnull=False),
                name="uniq_supplier_reversal_client_ref",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "reversed_at"],
                name="supp_reversal_biz_date_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("Payment reversals are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            "Payment reversals cannot be deleted."
        )

    def __str__(self):
        return f"Reversal of {self.payment.payment_number}"


class SupplierPaymentAllocationReversal(models.Model):
    """
    The part of an allocation taken back by a payment reversal.

    Effective allocation = allocation.amount - sum(these rows). The
    allocation row itself stays untouched.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="supplier_allocation_reversals",
    )
    reversal = models.ForeignKey(
        SupplierPaymentReversal,
        on_delete=models.PROTECT,
        related_name="unwinds",
    )
    allocation = models.ForeignKey(
        SupplierPaymentAllocation,
        on_delete=models.PROTECT,
        related_name="unwinds",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableLedgerManager()

    class Meta:
        db_table = "payments_supplier_allocation_reversal"
        constraints = [
            models.UniqueConstraint(
                fields=["reversal", "allocation"],
                name="uniq_alloc_reversal_per_reversal",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="supplier_alloc_reversal_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "allocation"],
                name="supp_allocrev_biz_alloc_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("Allocation reversals are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Allocation reversals cannot be deleted.")
