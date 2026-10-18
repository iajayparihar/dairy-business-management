from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.expenses import services
from apps.expenses.models import Expense
from apps.payments.services import ClientRefConflict
from tests.expenses.base import D, ExpBase


class ExpenseTests(ExpBase):
    def test_create_defaults_and_number(self):
        e = self.spend("15000.00")
        today = timezone.localdate()
        self.assertEqual(e.expense_date, today)
        self.assertEqual(e.payment_method, "CASH")
        self.assertEqual(e.expense_number, f"EXP-{today.strftime('%y%m%d')}-1")
        self.assertEqual(self.spend().expense_number, f"EXP-{today.strftime('%y%m%d')}-2")

    def test_number_is_never_reused_after_cancel(self):
        first = self.spend()
        services.cancel_expense(business_id=self.business.pk, expense_id=first.pk,
                                user=self.owner, reason="typo")
        self.assertTrue(self.spend().expense_number.endswith("-2"))

    def test_staff_cannot_record(self):
        with self.assertRaises(PermissionDenied):
            self.spend(user=self.staff)

    def test_bad_amounts(self):
        for bad in ("0", "-5", "abc", "NaN", "1.234", "1e30", None):
            with self.subTest(bad), self.assertRaises(ValidationError):
                self.spend(bad)

    def test_future_and_datetime_dates_rejected(self):
        with self.assertRaises(ValidationError):
            self.spend(expense_date=timezone.localdate() + timedelta(days=1))
        with self.assertRaises(ValidationError):
            self.spend(expense_date=timezone.now())

    def test_past_date_ok_and_number_uses_that_date(self):
        e = self.spend(expense_date=timezone.localdate() - timedelta(days=40))
        self.assertEqual(e.expense_number[4:10], e.expense_date.strftime("%y%m%d"))

    def test_category_must_be_active_and_same_business(self):
        other = services.create_category(
            business_id=self.other_business.pk, user=self.other_owner, name="Rent")
        with self.assertRaises(ValidationError):
            self.spend(category_id=other.pk)
        services.update_category(business_id=self.business.pk, category_id=self.electricity.pk,
                                 user=self.owner, is_active=False)
        with self.assertRaises(ValidationError):
            self.spend()

    def test_replay_same_ref_returns_original_and_ignores_defaulted_date(self):
        a = self.spend("50.00", client_ref="r1")
        b = self.spend("50.00", client_ref="r1")
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(Expense.objects.count(), 1)

    def test_same_ref_different_data_conflicts(self):
        self.spend("50.00", client_ref="r1")
        with self.assertRaises(ClientRefConflict):
            self.spend("51.00", client_ref="r1")
        with self.assertRaises(ClientRefConflict):
            self.spend("50.00", client_ref="r1", payment_method="UPI")
        with self.assertRaises(ClientRefConflict):
            self.spend("50.00", client_ref="r1",
                       expense_date=timezone.localdate() - timedelta(days=3))

    def test_cancel_keeps_audit_and_cannot_repeat(self):
        e = self.spend()
        services.cancel_expense(business_id=self.business.pk, expense_id=e.pk,
                                user=self.manager, reason="wrong entry")
        e.refresh_from_db()
        self.assertEqual((e.status, e.cancelled_by, e.cancel_reason),
                         ("CANCELLED", self.manager, "wrong entry"))
        self.assertIsNotNone(e.cancelled_at)
        with self.assertRaises(ValidationError):
            services.cancel_expense(business_id=self.business.pk, expense_id=e.pk,
                                    user=self.manager, reason="again")

    def test_cancel_needs_reason(self):
        e = self.spend()
        with self.assertRaises(ValidationError):
            services.cancel_expense(business_id=self.business.pk, expense_id=e.pk,
                                    user=self.manager, reason=" a ")

    def test_correct_keeps_date_links_replacement(self):
        day = timezone.localdate() - timedelta(days=5)
        old = self.spend("100.00", expense_date=day)
        new = services.correct_expense(
            business_id=self.business.pk, expense_id=old.pk, user=self.manager,
            reason="amount typo", amount="120.00")
        old.refresh_from_db()
        self.assertEqual(old.status, "CANCELLED")
        self.assertEqual((new.expense_date, new.amount, new.replaces_id), (day, D("120.00"), old.pk))
        self.assertNotEqual(new.expense_number, old.expense_number)
        # the cancelled original can no longer be corrected
        with self.assertRaises(ValidationError):
            services.correct_expense(business_id=self.business.pk, expense_id=old.pk,
                                     user=self.manager, reason="again", amount="1.00")

    def test_correct_rejects_date_and_empty(self):
        e = self.spend()
        for kw in ({"expense_date": timezone.localdate()}, {}):
            with self.subTest(kw), self.assertRaises(ValidationError):
                services.correct_expense(business_id=self.business.pk, expense_id=e.pk,
                                         user=self.manager, reason="fix", **kw)

    def test_other_business_expense_not_found(self):
        e = self.spend()
        with self.assertRaises(PermissionDenied):
            services.cancel_expense(business_id=self.other_business.pk, expense_id=e.pk,
                                    user=self.manager, reason="nope")
        with self.assertRaises(ValidationError):
            services.cancel_expense(business_id=self.other_business.pk, expense_id=e.pk,
                                    user=self.other_owner, reason="nope")

    def test_rows_are_immutable(self):
        e = self.spend()
        e.amount = D("1.00")
        with self.assertRaises(ValueError):
            e.save()
        with self.assertRaises(ValueError):
            e.delete()
        with self.assertRaises(ValueError):
            Expense.objects.all().update(amount=1)
        with self.assertRaises(ValueError):
            Expense.objects.all().delete()

    def test_category_names_unique_ignoring_case(self):
        with self.assertRaises(ValidationError):
            services.create_category(business_id=self.business.pk, user=self.owner,
                                     name="electricity ")
        services.create_category(business_id=self.other_business.pk,
                                 user=self.other_owner, name="Electricity")

    def test_summary_excludes_cancelled(self):
        self.spend("100.00")
        gone = self.spend("40.00")
        services.cancel_expense(business_id=self.business.pk, expense_id=gone.pk,
                                user=self.owner, reason="oops")
        today = timezone.localdate()
        rows = services.expense_summary(business_id=self.business.pk, start=today, end=today)
        self.assertEqual([(r["category__name"], r["total"], r["count"]) for r in rows],
                         [("Electricity", D("100.00"), 1)])
