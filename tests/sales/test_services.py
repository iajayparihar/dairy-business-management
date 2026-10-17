from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from apps.inventory.models import StockMovement
from apps.payments.services import ClientRefConflict
from apps.sales.models import Sale, SaleLine
from apps.sales.services import (
    cancel_sale,
    correct_sale,
    daily_summary,
    quick_create_customer,
)
from tests.sales.base import SalesBase, D


class ModelTests(SalesBase):
    def test_credit_needs_customer_in_db(self):
        sale = self.sell()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Sale.objects.filter(pk=sale.pk)  # queryset update is blocked...
            sale.payment_mode = "CREDIT"
            sale.save()

    def test_immutable_and_undeletable(self):
        sale = self.sell()
        line = sale.lines.get()
        with self.assertRaises(ValueError):
            line.save()
        with self.assertRaises(ValueError):
            line.delete()
        with self.assertRaises(ValueError):
            sale.delete()
        with self.assertRaises(ValueError):
            Sale.objects.all().delete()
        with self.assertRaises(ValueError):
            Sale.objects.all().update(notes="x")
        with self.assertRaises(ValueError):
            SaleLine.objects.all().delete()

    def test_other_item_shape_enforced(self):
        sale = self.sell()
        with self.assertRaises(IntegrityError), transaction.atomic():
            SaleLine.objects.create(
                business=self.business, sale=sale, position=9, product=None,
                description="", quantity=1, unit_price=D("5"),
            )


class CreateTests(SalesBase):
    def test_product_line_price_snapshot_and_total(self):
        sale = self.sell([{"product_id": self.ghee.pk, "quantity": "0.250"}])
        line = sale.lines.get()
        self.assertEqual(line.unit_price, D("800.00"))
        self.assertEqual(line.line_total, D("200.00"))
        self.assertEqual(sale.total_amount, D("200.00"))
        self.assertEqual(line.description, "Cow Ghee")

    def test_other_item_line(self):
        sale = self.sell([{"description": "Bread", "amount": "35.50"}])
        line = sale.lines.get()
        self.assertIsNone(line.product)
        self.assertEqual(sale.total_amount, D("35.50"))
        self.assertFalse(StockMovement.objects.exists())

    def test_mixed_lines_and_number(self):
        sale = self.sell([
            {"product_id": self.ghee.pk, "quantity": "1"},
            {"description": "Bag", "amount": "5"},
        ])
        self.assertEqual(sale.total_amount, D("805.00"))
        self.assertRegex(sale.sale_number, r"^SHOP-\d{6}-1$")
        second = self.sell()
        self.assertTrue(second.sale_number.endswith("-2"))

    def test_number_never_reused_after_cancel(self):
        first = self.sell()
        cancel_sale(business_id=self.business.pk, sale_id=first.pk,
                    user=self.owner, reason="oops")
        self.assertTrue(self.sell().sale_number.endswith("-2"))

    def test_price_resolved_at_sold_at(self):
        from datetime import datetime
        from apps.products.models import ProductPrice
        from tests.billing.base import IST
        cut = datetime(2026, 6, 1, tzinfo=IST)
        ProductPrice.objects.filter(product=self.ghee).update(effective_to=cut)
        ProductPrice.objects.create(
            business=self.business, product=self.ghee, price_type="SELLING",
            unit_price=D("900.00"), effective_from=cut,
        )
        line = [{"product_id": self.ghee.pk, "quantity": "1"}]
        old = self.sell(line, sold_at=datetime(2026, 3, 1, tzinfo=IST),
                        user=self.owner)
        new = self.sell(line)
        self.assertEqual(old.total_amount, D("800.00"))
        self.assertEqual(new.total_amount, D("900.00"))

    def test_shape_errors(self):
        bad = [
            [],
            [{"product_id": self.ghee.pk, "quantity": "1", "amount": "5"}],
            [{"description": "x", "amount": "5", "quantity": "1"}],
            [{"description": "", "amount": "5"}],
            [{"description": "x", "amount": "0"}],
            [{"description": "x", "amount": "1.234"}],
            [{"product_id": self.ghee.pk, "quantity": "0"}],
            [{"product_id": self.ghee.pk, "quantity": "1", "unit_price": "1"}],
        ]
        for lines in bad:
            with self.subTest(lines=lines), self.assertRaises(ValidationError):
                self.sell(lines)
        self.assertFalse(Sale.objects.exists())

    def test_whole_number_unit(self):
        pcs = self.product("EGG", "Egg", "6.00", unit="PCS")
        with self.assertRaises(ValidationError):
            self.sell([{"product_id": pcs.pk, "quantity": "1.5"}])

    def test_open_price_product(self):
        p = self.product("MISC", "Sweets", "1.00", unit="KG")
        p.allow_open_price = True
        p.save(update_fields=["allow_open_price"])
        with self.assertRaises(ValidationError):
            self.sell([{"product_id": p.pk, "quantity": "1"}])
        s = self.sell([{"product_id": p.pk, "quantity": "2", "unit_price": "120"}])
        self.assertEqual(s.total_amount, D("240.00"))

    def test_foreign_or_inactive_product_rejected(self):
        other = self.product("X", "X", "1", business=self.other_business)
        with self.assertRaises(ValidationError):
            self.sell([{"product_id": other.pk, "quantity": "1"}])
        self.ghee.is_active = False
        self.ghee.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            self.sell()

    def test_credit_rules(self):
        with self.assertRaises(ValidationError):
            self.sell(mode="CREDIT", customer_id=None)
        with self.assertRaises(ValidationError):
            self.sell(mode="CREDIT", customer_id=self.other_customer.pk)
        sale = self.sell(mode="CREDIT")
        self.assertEqual(sale.customer, self.customer)

    def test_cash_sale_may_name_a_customer_or_none(self):
        self.assertIsNone(self.sell().customer)
        self.assertEqual(
            self.sell(customer_id=self.customer.pk).customer, self.customer
        )

    def test_time_rules(self):
        with self.assertRaises(ValidationError):
            self.sell(sold_at=self.hours_ago(-2))  # future
        with self.assertRaises(ValidationError):
            self.sell(sold_at=self.hours_ago(49))  # staff too old
        self.sell(sold_at=self.hours_ago(49), user=self.manager)
        with self.assertRaises(ValidationError):
            self.sell(sold_at=self.hours_ago(1).replace(tzinfo=None))

    def test_invalid_mode_and_missing_ref(self):
        with self.assertRaises(ValidationError):
            self.sell(mode="BARTER")
        with self.assertRaises(ValidationError):
            self.sell(client_ref="")

    def test_outsider_denied(self):
        with self.assertRaises(PermissionDenied):
            self.sell(user=self.outsider)


class IdempotencyTests(SalesBase):
    def test_replay_returns_same_sale(self):
        a = self.sell(client_ref="r1")
        b = self.sell(client_ref="r1")
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_replay_with_explicit_matching_fields(self):
        at = self.hours_ago(2)
        a = self.sell(client_ref="r1", sold_at=at, notes="n")
        b = self.sell(client_ref="r1", sold_at=at, notes="n")
        self.assertEqual(a.pk, b.pk)

    def test_same_ref_different_data_conflicts(self):
        self.sell(client_ref="r1")
        cases = [
            dict(lines=[{"product_id": self.ghee.pk, "quantity": "2"}]),
            dict(mode="ONLINE"),
            dict(notes="different"),
            dict(customer_id=self.customer.pk),
            dict(sold_at=self.hours_ago(5)),
            dict(lines=[{"description": "x", "amount": "5"}]),
        ]
        for kw in cases:
            with self.subTest(kw=kw), self.assertRaises(ClientRefConflict):
                self.sell(client_ref="r1", **kw)

    def test_replay_survives_closed_window(self):
        # A retry is answered even if time rules would now refuse it.
        at = self.hours_ago(47)
        a = self.sell(client_ref="r1", sold_at=at)
        b = self.sell(client_ref="r1", sold_at=at, user=self.staff)
        self.assertEqual(a.pk, b.pk)


class StockTests(SalesBase):
    def stock_in(self, qty):
        StockMovement.objects.create(
            business=self.business, product=self.paneer,
            movement_type="ADJUSTMENT_IN", quantity=D(qty),
        )

    def test_sale_posts_movement_and_flags_shortfall(self):
        self.stock_in("1")
        sale = self.sell([{"product_id": self.paneer.pk, "quantity": "1.5"}])
        line = sale.lines.get()
        self.assertEqual(line.stock_shortfall, D("0.500"))
        mv = StockMovement.objects.get(movement_type="SALE")
        self.assertEqual((mv.sale_line, mv.quantity, mv.occurred_at),
                         (line, D("1.5"), sale.sold_at))

    def test_two_lines_same_product_share_stock(self):
        self.stock_in("1")
        sale = self.sell([
            {"product_id": self.paneer.pk, "quantity": "0.8"},
            {"product_id": self.paneer.pk, "quantity": "0.8"},
        ])
        self.assertEqual(
            [l.stock_shortfall for l in sale.lines.all()],
            [D("0"), D("0.600")],
        )

    def test_untracked_product_posts_nothing(self):
        self.sell()
        self.assertFalse(StockMovement.objects.exists())

    def test_cancel_reverses_stock(self):
        self.stock_in("2")
        sale = self.sell([{"product_id": self.paneer.pk, "quantity": "1"}])
        cancel_sale(business_id=self.business.pk, sale_id=sale.pk,
                    user=self.owner, reason="wrong")
        rev = StockMovement.objects.get(movement_type="REVERSAL_IN",
                                        sale_line__sale=sale)
        self.assertEqual(rev.occurred_at, sale.sold_at)
        from apps.inventory.services import stock_balance
        self.assertEqual(
            stock_balance(business_id=self.business.pk,
                          product_id=self.paneer.pk), D("2"))


class CancelCorrectTests(SalesBase):
    def test_roles(self):
        sale = self.sell()
        for user in (self.staff, self.outsider):
            with self.subTest(user=user), self.assertRaises(PermissionDenied):
                cancel_sale(business_id=self.business.pk, sale_id=sale.pk,
                            user=user, reason="x")

    def test_cancel_needs_reason_and_records_audit(self):
        sale = self.sell()
        with self.assertRaises(ValidationError):
            cancel_sale(business_id=self.business.pk, sale_id=sale.pk,
                        user=self.owner, reason=" ")
        cancel_sale(business_id=self.business.pk, sale_id=sale.pk,
                    user=self.manager, reason="mistake")
        sale.refresh_from_db()
        self.assertEqual(sale.status, "CANCELLED")
        self.assertEqual((sale.cancelled_by, sale.cancel_reason),
                         (self.manager, "mistake"))
        self.assertIsNotNone(sale.cancelled_at)
        with self.assertRaises(ValidationError):
            cancel_sale(business_id=self.business.pk, sale_id=sale.pk,
                        user=self.owner, reason="again")

    def test_correct_keeps_time_and_changes_mode(self):
        sale = self.sell(sold_at=self.hours_ago(30), user=self.owner)
        new = correct_sale(
            business_id=self.business.pk, sale_id=sale.pk, user=self.owner,
            reason="was credit", payment_mode="CREDIT",
            customer_id=self.customer.pk,
            lines=[{"product_id": self.ghee.pk, "quantity": "1"}],
        )
        sale.refresh_from_db()
        self.assertEqual(sale.status, "CANCELLED")
        self.assertEqual(new.replaces, sale)
        self.assertEqual(new.sold_at, sale.sold_at)
        self.assertEqual(new.total_amount, D("800.00"))
        self.assertEqual(new.customer, self.customer)

    def test_correct_cannot_swap_customer(self):
        sale = self.sell(mode="CREDIT")
        with self.assertRaises(ValidationError):
            correct_sale(
                business_id=self.business.pk, sale_id=sale.pk,
                user=self.owner, reason="x", payment_mode="CREDIT",
                customer_id=self.customer2.pk,
                lines=[{"description": "a", "amount": "1"}],
            )
        sale.refresh_from_db()
        self.assertEqual(sale.status, "COMPLETED")  # rolled back whole

    def test_failed_correction_rolls_back_cancel(self):
        sale = self.sell()
        with self.assertRaises(ValidationError):
            correct_sale(
                business_id=self.business.pk, sale_id=sale.pk,
                user=self.owner, reason="x", payment_mode="CREDIT",
                lines=[{"description": "a", "amount": "1"}],  # no customer
            )
        sale.refresh_from_db()
        self.assertEqual(sale.status, "COMPLETED")


class QuickCustomerAndSummaryTests(SalesBase):
    def test_quick_customer_codes(self):
        a = quick_create_customer(business_id=self.business.pk,
                                  user=self.staff, name="Mohan", phone="99")
        b = quick_create_customer(business_id=self.business.pk,
                                  user=self.staff, name="Geeta", phone="98")
        self.assertEqual((a.customer_code, b.customer_code),
                         ("CUST-1", "CUST-2"))
        with self.assertRaises(ValidationError):
            quick_create_customer(business_id=self.business.pk,
                                  user=self.staff, name="", phone="1")
        with self.assertRaises(ValidationError):
            quick_create_customer(business_id=self.business.pk,
                                  user=self.staff, name="A", phone="")
        with self.assertRaises(PermissionDenied):
            quick_create_customer(business_id=self.business.pk,
                                  user=self.outsider, name="A", phone="1")

    def test_daily_summary(self):
        from django.utils import timezone as tz
        self.sell([{"description": "a", "amount": "10"}])
        self.sell([{"description": "b", "amount": "20"}], mode="ONLINE")
        gone = self.sell([{"description": "c", "amount": "99"}])
        cancel_sale(business_id=self.business.pk, sale_id=gone.pk,
                    user=self.owner, reason="x")
        today = tz.localtime(Sale.objects.first().sold_at).date()
        out = daily_summary(business_id=self.business.pk, user=self.staff,
                            day=today)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["total"], D("30.00"))
        self.assertEqual(out["by_payment_mode"]["CASH"]["total"], D("10.00"))
