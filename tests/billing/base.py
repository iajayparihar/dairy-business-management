"""Shared fixtures for the billing tests."""
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.billing.bill_services import finalize_bill, generate_draft_bill, void_bill
from apps.billing.models import (
    CustomerBill,
    CustomerBillLine,
    CustomerPayment,
    CustomerPaymentAllocation,
)
from apps.billing.payment_services import record_customer_payment
from apps.billing.selectors import get_customer_position
from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.customers.models import Customer
from apps.deliveries.services import create_delivery
from apps.inventory.models import BusinessMilkConfig
from apps.products.models import Product, ProductPrice

User = get_user_model()
D = Decimal
IST = ZoneInfo("Asia/Kolkata")

# All fixture dates are in the past, whatever day the suite runs.
PRICE_FROM = datetime(2026, 1, 1, tzinfo=IST)
SEP = (date(2026, 9, 1), date(2026, 9, 30))


def ist(year, month, day, hour=6, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=IST)


class BillingBase:
    """
    Business "Bill Dairy" with an owner, a manager and a STAFF delivery boy,
    two customers, cow / buffalo milk and one extra product, and the
    cow / buffalo milk mapping. A second business exists for tenant tests.

    Usernames and slugs are unique to billing, so this mixin can sit next to
    another test base.
    """

    def build(self):
        mk = lambda name: User.objects.create_user(
            username=name, password="x-pass-123",
        )
        self.owner = mk("bl_owner")
        self.manager = mk("bl_manager")
        self.staff = mk("bl_staff")
        self.outsider = mk("bl_outsider")
        self.other_owner = mk("bl_other_owner")

        self.business = create_business(
            name="Bill Dairy", slug="bill-dairy", owner=self.owner,
        )
        BusinessMembership.objects.create(
            business=self.business, user=self.manager, role="MANAGER",
        )
        BusinessMembership.objects.create(
            business=self.business, user=self.staff, role="STAFF",
        )
        self.other_business = create_business(
            name="Other Dairy", slug="other-bill-dairy", owner=self.other_owner,
        )

        self.customer = Customer.objects.create(
            business=self.business, customer_code="C001", name="Ravi",
            address="12 Main Road",
        )
        self.customer2 = Customer.objects.create(
            business=self.business, customer_code="C002", name="Sita",
        )
        self.other_customer = Customer.objects.create(
            business=self.other_business, customer_code="C001", name="Other",
        )

        self.cow = self.product("COW", "Cow Milk", "60.00")
        self.buffalo = self.product("BUF", "Buffalo Milk", "80.00")
        self.ghee = self.product("GHEE", "Cow Ghee", "800.00", unit="KG")
        BusinessMilkConfig.objects.create(
            business=self.business,
            cow_product=self.cow,
            buffalo_product=self.buffalo,
        )

        self._bill_seq = 0
        self._pay_seq = 0

    def product(self, sku, name, price, unit="L", business=None):
        business = business or self.business
        product = Product.objects.create(
            business=business, sku=sku, name=name, unit=unit,
        )
        ProductPrice.objects.create(
            business=business,
            product=product,
            price_type="SELLING",
            unit_price=Decimal(price),
            effective_from=PRICE_FROM,
        )
        return product

    def deliver(self, product, quantity, at, customer=None, shift="MORNING",
                user=None, **kwargs):
        """Record a real delivery through the service (price snapshot, rules)."""
        customer = customer or self.customer
        return create_delivery(
            business_id=customer.business_id,
            customer_id=customer.pk,
            product_id=product.pk,
            quantity=quantity,
            delivered_at=at,
            shift=shift,
            user=user or self.owner,
            **kwargs,
        )

    def make_bill(self, *, customer=None, start=date(2026, 9, 1),
                  end=date(2026, 9, 30), status="DRAFT", total="0.00",
                  number=None, bill_type="PERIOD", **extra):
        """Insert a bill row directly, with the stamps its status requires."""
        customer = customer or self.customer
        self._bill_seq += 1
        fields = dict(
            business_id=customer.business_id,
            customer=customer,
            bill_type=bill_type,
            bill_number=number or f"T-{self._bill_seq:04d}",
            period_start=start,
            period_end=end,
            status=status,
            total_amount=Decimal(total),
            created_by=self.owner,
        )
        if status == "FINALIZED":
            fields.update(
                finalized_at=timezone.now(),
                finalized_by=self.owner,
                balance_before=Decimal("0.00"),
            )
        elif status == "VOID":
            fields.update(
                voided_at=timezone.now(),
                voided_by=self.owner,
                void_reason="test void",
            )
        fields.update(extra)
        return CustomerBill.objects.create(**fields)

    def pay(self, amount, *, customer=None, user=None, **kwargs):
        """Record a payment through the service (numbering, allocation)."""
        customer = customer or self.customer
        return record_customer_payment(
            business_id=customer.business_id,
            customer_id=customer.pk,
            amount=amount,
            user=user or self.owner,
            **kwargs,
        )

    def position(self, customer=None, as_of=None):
        customer = customer or self.customer
        return get_customer_position(
            business_id=customer.business_id,
            customer_id=customer.pk,
            as_of=as_of,
        )

    def assert_reconciled(self, customer=None, as_of=None):
        """net balance == outstanding - advance, and neither is negative."""
        position = self.position(customer, as_of)
        self.assertEqual(
            position["net_balance"],
            position["outstanding_amount"] - position["available_credit"],
        )
        self.assertGreaterEqual(position["outstanding_amount"], 0)
        self.assertGreaterEqual(position["available_credit"], 0)
        return position

    def make_payment(self, *, customer=None, amount="100.00", paid_at=None,
                     number=None, **extra):
        """Insert a payment row directly (no allocation)."""
        customer = customer or self.customer
        self._pay_seq += 1
        return CustomerPayment.objects.create(
            business_id=customer.business_id,
            customer=customer,
            payment_number=number or f"TP-{self._pay_seq:04d}",
            paid_at=paid_at or timezone.now(),
            amount=Decimal(amount),
            created_by=self.owner,
            **extra,
        )


class BillFlow(BillingBase):
    """BillingBase plus the bill lifecycle helpers (through the services)."""

    def setUp(self):
        self.build()

    def draft(self, period=SEP, customer=None, user=None):
        customer = customer or self.customer
        return generate_draft_bill(
            business_id=customer.business_id, customer_id=customer.pk,
            period_start=period[0], period_end=period[1],
            user=user or self.owner,
        )

    def finalize(self, bill, user=None, **kwargs):
        return finalize_bill(
            business_id=bill.business_id, bill_id=bill.pk,
            user=user or self.owner, **kwargs,
        )

    def void(self, bill, reason="wrong delivery", user=None, **kwargs):
        return void_bill(
            business_id=bill.business_id, bill_id=bill.pk, reason=reason,
            user=user or self.owner, **kwargs,
        )

    def september(self, customer=None):
        """Cow 2 L x3 days (60), buffalo 1 L (80) and 1 kg ghee (800)."""
        customer = customer or self.customer
        # Distinct times, so the order of the bill's lines is deterministic.
        for day in (1, 2, 3):
            self.deliver(self.cow, "2", ist(2026, 9, day), customer=customer)
        self.deliver(self.buffalo, "1", ist(2026, 9, 2, 6, 30), customer=customer)
        self.deliver(self.ghee, "1", ist(2026, 9, 3, 7, 0), customer=customer)
        return D("360.00") + D("80.00") + D("800.00")  # 1240.00

    def lines(self, bill):
        return list(CustomerBillLine.objects.filter(bill=bill))

    def paid_on(self, bill):
        return sum(
            (a.amount for a in CustomerPaymentAllocation.objects.filter(bill=bill)),
            D("0"),
        )
