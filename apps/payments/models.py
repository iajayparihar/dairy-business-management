
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from apps.businesses.models import Business
from apps.purchases.models import SupplierPurchase
from apps.suppliers.models import Supplier


class SupplierPayment(models.Model):
    class PaymentMethod(models.TextChoices):
        CASH = "CASH", "Cash"
        UPI = "UPI", "UPI"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"
        CHEQUE = "CHEQUE", "Cheque"
        OTHER = "OTHER", "Other"

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
