"""
Customer receivables: bills, customer payments and their allocations.

A FINALIZED CustomerBill is the charge. Deliveries are only its source, so
they never count in a balance until a bill covers them. Payments, allocations
and reversals mirror the supplier design (apps/payments); allocations on a
VOID bill are ignored, which returns that money to the customer's credit.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Func, Q, Value

from apps.businesses.models import Business
from apps.common.validators import validate_same_business
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.payments.models import ImmutableLedgerManager, ImmutableLedgerQuerySet
from apps.products.models import Product


class CustomerBillQuerySet(models.QuerySet):
    """A bill changes state one row at a time, through the bill service."""

    def update(self, **kwargs):
        raise ValueError("Bills cannot be bulk-updated.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("Bills cannot be bulk-updated.")

    def delete(self):
        raise ValueError("Bills cannot be deleted. Void the bill instead.")


class BillLineQuerySet(ImmutableLedgerQuerySet):
    """Lines are immutable; only the finalize service inserts them, in bulk."""

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        # Skip the ledger block, which exists for rows written one at a time.
        return super(ImmutableLedgerQuerySet, self).bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


CustomerBillManager = models.Manager.from_queryset(CustomerBillQuerySet)
BillLineManager = models.Manager.from_queryset(BillLineQuerySet)


class CustomerBill(models.Model):
    """
    One receivable document for a customer.

    PERIOD: the charges for [period_start, period_end] (inclusive local dates
    in Business.timezone). OPENING: the balance brought forward from the paper
    register (single day).

    DRAFT -> FINALIZED -> VOID, or DRAFT -> VOID. A finalized bill never
    changes except to be voided (reason required); a replacement is a new
    bill linked through `replaces`.
    """

    class BillType(models.TextChoices):
        PERIOD = "PERIOD", "Period bill"
        OPENING = "OPENING", "Opening balance"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        FINALIZED = "FINALIZED", "Finalized"
        VOID = "VOID", "Void"

    # The only columns a FINALIZED bill may change, and only to become VOID.
    VOID_FIELDS = frozenset({
        "status", "voided_at", "voided_by", "void_reason", "updated_at",
    })

    objects = CustomerBillManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="customer_bills",
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="bills",
    )
    bill_type = models.CharField(
        max_length=10, choices=BillType.choices, default=BillType.PERIOD,
    )
    # Server-generated, never reused: <CUSTOMER_CODE>-<YYMMDD>-<seq>.
    bill_number = models.CharField(max_length=50)
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT,
    )
    # Meaningful from FINALIZED on; a DRAFT shows its live total instead.
    total_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    # Customer net balance (+ owes, - advance) just before this bill.
    balance_before = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
    )
    # Business and customer identity at finalize, so the printed bill never
    # changes when a name or address is edited later.
    header_snapshot = models.JSONField(default=dict, blank=True)

    finalized_at = models.DateTimeField(null=True, blank=True)
    finalized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    void_reason = models.TextField(blank=True, default="")
    replaces = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replaced_by",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_customer_bill"
        ordering = ["-period_start", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "bill_number"],
                name="uniq_customer_bill_number",
            ),
            # One live bill per customer and exact period.
            models.UniqueConstraint(
                fields=["business", "customer", "period_start", "period_end"],
                condition=Q(
                    bill_type="PERIOD", status__in=["DRAFT", "FINALIZED"],
                ),
                name="uniq_customer_bill_live_period",
            ),
            models.UniqueConstraint(
                fields=["business", "customer"],
                condition=Q(bill_type="OPENING", status="FINALIZED"),
                name="uniq_customer_bill_live_opening",
            ),
            # Final guard against billing a delivery twice: finalized
            # periods of one customer never overlap.
            ExclusionConstraint(
                name="excl_customer_bill_final_overlap",
                expressions=[
                    ("business", RangeOperators.EQUAL),
                    ("customer", RangeOperators.EQUAL),
                    (
                        Func(
                            F("period_start"),
                            F("period_end"),
                            Value("[]"),
                            function="DATERANGE",
                            output_field=DateRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
                condition=Q(bill_type="PERIOD", status="FINALIZED"),
            ),
            models.CheckConstraint(
                condition=Q(period_end__gte=F("period_start")),
                name="customer_bill_period_valid",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=0),
                name="customer_bill_total_nonneg",
            ),
            models.CheckConstraint(
                condition=(
                    Q(bill_type="PERIOD")
                    | (
                        Q(period_start=F("period_end"))
                        & Q(status__in=["FINALIZED", "VOID"])
                    )
                ),
                name="customer_bill_opening_shape",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="DRAFT",
                        finalized_at__isnull=True,
                        voided_at__isnull=True,
                    )
                    | Q(
                        status="FINALIZED",
                        finalized_at__isnull=False,
                        voided_at__isnull=True,
                        balance_before__isnull=False,
                    )
                    | (
                        Q(status="VOID", voided_at__isnull=False)
                        & ~Q(void_reason="")
                    )
                ),
                name="customer_bill_state_consistent",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "customer", "status", "period_start"],
                name="cbill_cust_status_idx",
            ),
            models.Index(
                fields=["business", "status", "finalized_at"],
                name="cbill_biz_status_final_idx",
            ),
        ]

    def clean(self):
        super().clean()
        validate_same_business(self, self.customer)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = (
                type(self)._base_manager.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )

            if stored == self.Status.VOID:
                raise ValueError("A void bill cannot be changed.")

            if stored == self.Status.FINALIZED:
                update_fields = kwargs.get("update_fields")
                if (
                    self.status != self.Status.VOID
                    or update_fields is None
                    or not set(update_fields) <= self.VOID_FIELDS
                ):
                    raise ValueError(
                        "A finalized bill cannot be edited. Void it and "
                        "issue a replacement."
                    )

        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Bills cannot be deleted. Void the bill instead.")

    def __str__(self):
        return f"{self.bill_number} ({self.status})"


class CustomerBillLine(models.Model):
    """
    One delivery or shop-sale line on a finalized bill, copied as it was
    when billed.

    amount is the source's stored line_total, never quantity x unit_price
    recomputed, so rounding and old prices stay exactly as delivered.
    """

    class Kind(models.TextChoices):
        COW_MILK = "COW_MILK", "Cow milk"
        BUFFALO_MILK = "BUFFALO_MILK", "Buffalo milk"
        OTHER = "OTHER", "Other items"

    objects = BillLineManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="customer_bill_lines",
    )
    bill = models.ForeignKey(
        CustomerBill, on_delete=models.PROTECT, related_name="lines",
    )
    # Exactly one source: a milk delivery or a shop credit-sale line.
    delivery = models.ForeignKey(
        Delivery,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bill_lines",
    )
    sale_line = models.ForeignKey(
        "sales.SaleLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bill_lines",
    )
    # Null for a shop "other item" (free text, no product).
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+",
    )
    # Snapshot of the milk-config mapping at billing time.
    line_kind = models.CharField(max_length=15, choices=Kind.choices)
    description = models.CharField(max_length=200)
    unit = models.CharField(max_length=10)
    # Delivery time, or sold_at for a shop line.
    delivered_at = models.DateTimeField()
    # Blank for shop lines.
    shift = models.CharField(max_length=10, blank=True, default="")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_customer_bill_line"
        ordering = ["delivered_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["bill", "delivery"],
                condition=Q(delivery__isnull=False),
                name="uniq_bill_line_delivery",
            ),
            models.UniqueConstraint(
                fields=["bill", "sale_line"],
                condition=Q(sale_line__isnull=False),
                name="uniq_bill_line_sale_line",
            ),
            models.CheckConstraint(
                condition=(
                    Q(delivery__isnull=False, sale_line__isnull=True)
                    | Q(delivery__isnull=True, sale_line__isnull=False)
                ),
                name="bill_line_one_source",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="bill_line_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0),
                name="bill_line_price_nonneg",
            ),
            models.CheckConstraint(
                condition=Q(amount__gte=0),
                name="bill_line_amount_nonneg",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "delivery"],
                name="bline_biz_delivery_idx",
            ),
            models.Index(
                fields=["business", "sale_line"],
                name="bline_biz_sale_line_idx",
            ),
            models.Index(
                fields=["bill", "delivered_at"],
                name="bline_bill_time_idx",
            ),
        ]

    def clean(self):
        super().clean()
        validate_same_business(
            self, self.bill, self.delivery, self.sale_line, self.product
        )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Bill lines are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Bill lines cannot be deleted.")


class CustomerBillCancelledLine(models.Model):
    """
    A delivery or shop credit-sale line that was CANCELLED (or corrected) inside the bill's
    period, copied when the bill was finalized. It is shown beside the live lines so the
    customer and the owner see both the wrong and the right entry, but it is never part of
    the total, the balance or any allocation.
    """

    objects = BillLineManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="customer_bill_cancelled_lines",
    )
    bill = models.ForeignKey(
        CustomerBill, on_delete=models.PROTECT, related_name="cancelled_lines",
    )
    delivery = models.ForeignKey(
        Delivery, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    sale_line = models.ForeignKey(
        "sales.SaleLine", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    line_kind = models.CharField(max_length=15, choices=CustomerBillLine.Kind.choices)
    description = models.CharField(max_length=200)
    unit = models.CharField(max_length=10)
    delivered_at = models.DateTimeField()
    shift = models.CharField(max_length=10, blank=True, default="")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    # What the cancelled entry would have charged; informational only.
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_customer_bill_cancelled_line"
        ordering = ["delivered_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["bill", "delivery"], condition=Q(delivery__isnull=False),
                name="uniq_bill_cancelled_delivery",
            ),
            models.UniqueConstraint(
                fields=["bill", "sale_line"], condition=Q(sale_line__isnull=False),
                name="uniq_bill_cancelled_sale_line",
            ),
            models.CheckConstraint(
                condition=(
                    Q(delivery__isnull=False, sale_line__isnull=True)
                    | Q(delivery__isnull=True, sale_line__isnull=False)
                ),
                name="bill_cancelled_line_one_source",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Bill lines are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Bill lines cannot be deleted.")


class CustomerPayment(models.Model):
    """Money received from a customer. Immutable; fix with a reversal."""

    class PaymentMethod(models.TextChoices):
        CASH = "CASH", "Cash"
        UPI = "UPI", "UPI"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"
        CHEQUE = "CHEQUE", "Cheque"
        OTHER = "OTHER", "Other"

    objects = ImmutableLedgerManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business, on_delete=models.PROTECT, related_name="customer_payments",
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="payments",
    )
    # Server-generated, never reused: PAY-<CUSTOMER_CODE>-<YYMMDD>-<seq>.
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
    reference_number = models.CharField(max_length=100, blank=True, default="")
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
        db_table = "billing_customer_payment"
        ordering = ["-paid_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "payment_number"],
                name="uniq_customer_payment_number",
            ),
            models.UniqueConstraint(
                fields=["business", "client_ref"],
                condition=Q(client_ref__isnull=False),
                name="uniq_customer_payment_client_ref",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="customer_payment_amount_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "customer", "paid_at"],
                name="cpay_biz_cust_paid_idx",
            ),
        ]

    def clean(self):
        super().clean()
        validate_same_business(self, self.customer)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Posted customer payments are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            "Customer payments cannot be deleted. Use a reversal."
        )

    def __str__(self):
        return f"{self.payment_number} - {self.amount}"


class CustomerPaymentAllocation(models.Model):
    """
    Applies part of a payment to a finalized bill. What is not allocated
    stays as the customer's advance.
    """

    objects = ImmutableLedgerManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="customer_payment_allocations",
    )
    payment = models.ForeignKey(
        CustomerPayment, on_delete=models.PROTECT, related_name="allocations",
    )
    bill = models.ForeignKey(
        CustomerBill,
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
        db_table = "billing_customer_payment_allocation"
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "bill"],
                name="uniq_customer_payment_bill",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="customer_allocation_amount_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "bill"],
                name="calloc_biz_bill_idx",
            ),
        ]

    def clean(self):
        super().clean()
        validate_same_business(self, self.payment, self.bill)

        if self.payment.customer_id != self.bill.customer_id:
            raise ValidationError(
                "Payment and bill must belong to the same customer."
            )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Payment allocations are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            "Payment allocations cannot be deleted. Use a reversal."
        )

    def __str__(self):
        return f"{self.payment.payment_number} -> {self.bill.bill_number}"


class CustomerPaymentReversal(models.Model):
    """
    One (full or partial) reversal of a customer payment. A payment can have
    several; together they never exceed it (checked under the lock). The part
    that had been allocated is recorded as CustomerPaymentAllocationReversal
    rows; the rest came out of the unallocated advance.
    """

    objects = ImmutableLedgerManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="customer_payment_reversals",
    )
    payment = models.ForeignKey(
        CustomerPayment, on_delete=models.PROTECT, related_name="reversals",
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

    class Meta:
        db_table = "billing_customer_payment_reversal"
        ordering = ["-reversed_at", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="customer_reversal_amount_positive",
            ),
            models.CheckConstraint(
                condition=~Q(reason=""),
                name="customer_reversal_needs_reason",
            ),
            models.UniqueConstraint(
                fields=["business", "client_ref"],
                condition=Q(client_ref__isnull=False),
                name="uniq_customer_reversal_client_ref",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "reversed_at"],
                name="crev_biz_date_idx",
            ),
        ]

    def clean(self):
        super().clean()
        validate_same_business(self, self.payment)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Payment reversals are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Payment reversals cannot be deleted.")

    def __str__(self):
        return f"Reversal of {self.payment.payment_number}"


class CustomerPaymentAllocationReversal(models.Model):
    """
    The part of an allocation taken back by a payment reversal.

    Effective allocation = allocation.amount - sum(these rows). The
    allocation row itself stays untouched.
    """

    objects = ImmutableLedgerManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="customer_allocation_reversals",
    )
    reversal = models.ForeignKey(
        CustomerPaymentReversal,
        on_delete=models.PROTECT,
        related_name="unwinds",
    )
    allocation = models.ForeignKey(
        CustomerPaymentAllocation,
        on_delete=models.PROTECT,
        related_name="unwinds",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_customer_allocation_reversal"
        constraints = [
            models.UniqueConstraint(
                fields=["reversal", "allocation"],
                name="uniq_calloc_reversal_per_reversal",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="customer_alloc_reversal_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "allocation"],
                name="callocrev_biz_alloc_idx",
            ),
        ]

    def clean(self):
        super().clean()
        validate_same_business(self, self.reversal, self.allocation)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Allocation reversals are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Allocation reversals cannot be deleted.")
