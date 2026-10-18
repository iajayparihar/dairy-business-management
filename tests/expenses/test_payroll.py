from datetime import date, datetime, timedelta, timezone as dt_tz

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.expenses import payroll
from apps.expenses.models import SalaryAccrual
from apps.expenses.selectors import (
    employee_balances, employee_position, employee_statement, prorated_salary,
    salary_cost_by_month,
)
from apps.payments.services import ClientRefConflict
from tests.expenses.base import D, ExpBase, first_of_this_month, previous_month

NOV = date(2025, 11, 1)


class ProrataTests(ExpBase):
    def test_table(self):
        s = D("12000.00")
        cases = [
            (date(2025, 1, 1), None, NOV, D("12000.00"), 30),     # full month
            (date(2025, 11, 20), None, NOV, D("4400.00"), 11),    # joins the 20th
            (date(2025, 1, 1), date(2025, 11, 10), NOV, D("4000.00"), 10),  # leaves the 10th
            (date(2025, 11, 5), date(2025, 11, 5), NOV, D("400.00"), 1),    # one day
            (date(2025, 11, 10), date(2025, 11, 20), NOV, D("4400.00"), 11),
            (date(2020, 1, 1), None, date(2024, 2, 1), D("12000.00"), 29),  # leap Feb
            (date(2024, 2, 15), None, date(2024, 2, 1), D("6206.90"), 15),  # 12000*15/29 rounded
            (date(2025, 1, 1), None, date(2024, 2, 1), D("0.00"), 0),       # not joined yet
        ]
        for joined, left, month, expected, days in cases:
            with self.subTest(joined=joined, left=left, month=month):
                amount, d, dim = prorated_salary(s, joined, left, month)
                self.assertEqual(d, days)
                self.assertEqual(amount, expected)


class SalaryLedgerTests(ExpBase):
    def test_example_12k_due_15k_paid_is_3k_advance(self):
        emp = self.hire()
        self.generate(previous_month())
        self.pay_emp(emp, "15000.00")
        pos = employee_position(emp)
        self.assertEqual(pos["balance"], D("-3000.00"))
        self.assertTrue(pos["is_advance"])
        # next month the advance is netted
        self.generate(first_of_this_month())
        pos = employee_position(emp)
        self.assertEqual((pos["balance"], pos["is_advance"]), (D("9000.00"), False))

    def test_mid_month_advance_is_netted(self):
        emp = self.hire()
        self.pay_emp(emp, "5000.00")
        self.generate(previous_month())
        self.assertEqual(employee_position(emp)["balance"], D("7000.00"))

    def test_generate_twice_creates_once(self):
        emp = self.hire()
        first = self.generate(previous_month())
        second = self.generate(previous_month())
        self.assertEqual(len(first["created"]), 1)
        self.assertEqual(second["created"], [])
        self.assertEqual(second["skipped"][0]["reason"], "Already generated.")
        self.assertEqual(SalaryAccrual.objects.filter(employee=emp).count(), 1)

    def test_salary_change_does_not_touch_old_months(self):
        emp = self.hire()
        self.generate(previous_month())
        payroll.update_employee(business_id=self.business.pk, employee_id=emp.pk,
                                user=self.owner, monthly_salary="20000.00")
        self.assertEqual(employee_position(emp)["balance"], D("12000.00"))
        self.generate(first_of_this_month())
        self.assertEqual(employee_position(emp)["balance"], D("32000.00"))

    def test_skip_reasons(self):
        new = self.hire("New", joined=first_of_this_month())
        gone = self.hire("Gone", joined=date(2020, 1, 1))
        payroll.update_employee(business_id=self.business.pk, employee_id=gone.pk,
                                user=self.owner, left_on=date(2020, 6, 1), is_active=False)
        paused = self.hire("Paused")
        payroll.update_employee(business_id=self.business.pk, employee_id=paused.pk,
                                user=self.owner, is_active=False)
        out = self.generate(previous_month())
        reasons = {s["employee"].name: s["reason"] for s in out["skipped"]}
        self.assertEqual(reasons, {
            "New": "Employee had not joined yet.",
            "Gone": "Employee had already left.",
            "Paused": "Employee is inactive.",
        })
        self.assertEqual(out["created"], [])
        self.assertTrue(new and gone)

    def test_leaver_still_gets_final_month(self):
        emp = self.hire(joined=date(2020, 1, 1))
        last = previous_month().replace(day=10)
        payroll.update_employee(business_id=self.business.pk, employee_id=emp.pk,
                                user=self.owner, left_on=last, is_active=False)
        acc = self.generate(previous_month())["created"][0]
        self.assertEqual((acc.days_worked, acc.amount), (10, acc.amount))
        self.assertEqual(acc.amount, (D("12000.00") * 10 / acc.days_in_month).quantize(D("0.01")))

    def test_future_or_mid_month_value_rejected(self):
        self.hire()
        for bad in (date(2999, 1, 1), previous_month().replace(day=2), timezone.now()):
            with self.subTest(bad), self.assertRaises(ValidationError):
                self.generate(bad)

    def test_unknown_employee_ids_rejected(self):
        other = payroll.create_employee(
            business_id=self.other_business.pk, user=self.other_owner, name="X",
            monthly_salary="100", joined_on=date(2025, 1, 1))
        with self.assertRaises(ValidationError):
            self.generate(previous_month(), employee_ids=[other.pk])
        with self.assertRaises(ValidationError):
            self.generate(previous_month(), employee_ids=["not-a-uuid"])

    def test_generate_only_requested_employees(self):
        a, b = self.hire("A"), self.hire("B")
        out = self.generate(previous_month(), employee_ids=[a.pk])
        self.assertEqual([x.employee_id for x in out["created"]], [a.pk])
        self.assertEqual(employee_position(b)["balance"], D("0.00"))

    def test_bonus_and_deduction(self):
        emp = self.hire()
        self.generate(previous_month())
        self.adjust(emp, "BONUS", previous_month(), "2000.00", reason="Diwali")
        self.adjust(emp, "DEDUCTION", previous_month(), "800.00", reason="2 days absent")
        pos = employee_position(emp)
        self.assertEqual((pos["bonus"], pos["deductions"], pos["balance"]),
                         (D("2000.00"), D("800.00"), D("13200.00")))
        self.assertEqual(salary_cost_by_month(self.business.pk, previous_month(), previous_month()),
                         {previous_month(): D("13200.00")})

    def test_adjustment_replay_and_conflict_and_reason(self):
        emp = self.hire()
        a = self.adjust(emp, "BONUS", previous_month(), "10", client_ref="b1")
        b = self.adjust(emp, "BONUS", previous_month(), "10", client_ref="b1")
        self.assertEqual(a.pk, b.pk)
        with self.assertRaises(ClientRefConflict):
            self.adjust(emp, "BONUS", previous_month(), "11", client_ref="b1")
        with self.assertRaises(ValidationError):
            self.adjust(emp, "BONUS", previous_month(), "10", reason="")
        with self.assertRaises(ValidationError):
            self.adjust(emp, "SALARY", previous_month(), "10")

    def test_cancel_accrual_and_payment_restore_balance(self):
        emp = self.hire()
        acc = self.generate(previous_month())["created"][0]
        pay = self.pay_emp(emp, "5000.00")
        payroll.cancel_salary_payment(business_id=self.business.pk, payment_id=pay.pk,
                                      user=self.owner, reason="wrong employee")
        self.assertEqual(employee_position(emp)["balance"], D("12000.00"))
        payroll.cancel_accrual(business_id=self.business.pk, accrual_id=acc.pk,
                               user=self.owner, reason="generated by mistake")
        self.assertEqual(employee_position(emp)["balance"], D("0.00"))
        # a cancelled month can be generated again
        self.assertEqual(len(self.generate(previous_month())["created"]), 1)
        for fn, kw in ((payroll.cancel_salary_payment, {"payment_id": pay.pk}),
                       (payroll.cancel_accrual, {"accrual_id": acc.pk})):
            with self.assertRaises(ValidationError):
                fn(business_id=self.business.pk, user=self.owner, reason="again", **kw)

    def test_cancelling_a_paid_accrual_becomes_advance(self):
        emp = self.hire()
        acc = self.generate(previous_month())["created"][0]
        self.pay_emp(emp, "12000.00")
        payroll.cancel_accrual(business_id=self.business.pk, accrual_id=acc.pk,
                               user=self.owner, reason="duplicate")
        self.assertEqual(employee_position(emp)["balance"], D("-12000.00"))

    def test_payment_replay_conflict_number_and_future(self):
        emp = self.hire()
        a = self.pay_emp(emp, "100.00", client_ref="p1")
        self.assertEqual(self.pay_emp(emp, "100.00", client_ref="p1").pk, a.pk)
        with self.assertRaises(ClientRefConflict):
            self.pay_emp(emp, "101.00", client_ref="p1")
        self.assertRegex(a.payment_number, rf"^SAL-{emp.code}-\d{{6}}-1$")
        self.assertTrue(self.pay_emp(emp, "5").payment_number.endswith("-2"))
        with self.assertRaises(ValidationError):
            self.pay_emp(emp, "5", paid_at=timezone.now() + timedelta(hours=1))
        with self.assertRaises(ValidationError):
            self.pay_emp(emp, "5", paid_at=datetime(2026, 1, 1))  # naive

    def test_inactive_employee_can_still_be_paid(self):
        emp = self.hire()
        payroll.update_employee(business_id=self.business.pk, employee_id=emp.pk,
                                user=self.owner, is_active=False)
        self.pay_emp(emp, "100.00")

    def test_only_owner(self):
        emp = self.hire()
        for user in (self.manager, self.staff):
            with self.subTest(user=user.username):
                with self.assertRaises(PermissionDenied):
                    payroll.create_employee(business_id=self.business.pk, user=user, name="X",
                                            monthly_salary="1", joined_on=date(2025, 1, 1))
                with self.assertRaises(PermissionDenied):
                    payroll.generate_salary(business_id=self.business.pk, user=user,
                                            month=previous_month())
                with self.assertRaises(PermissionDenied):
                    payroll.create_salary_payment(
                        business_id=self.business.pk, user=user, employee_id=emp.pk,
                        client_ref="x", amount="1")

    def test_other_business_employee_not_found(self):
        emp = self.hire()
        with self.assertRaises(ValidationError):
            payroll.create_salary_payment(
                business_id=self.other_business.pk, user=self.other_owner,
                employee_id=emp.pk, client_ref="x", amount="1")

    def test_employee_validation(self):
        for kw in ({"name": " "}, {"salary": "0"}, {"salary": "-1"},
                   {"joined": "2025-01-01"}):
            with self.subTest(kw), self.assertRaises(ValidationError):
                self.hire(**{k: v for k, v in kw.items() if k != "name"} if "name" not in kw
                          else {"name": kw["name"]})
        emp = self.hire()
        with self.assertRaises(ValidationError):
            payroll.update_employee(business_id=self.business.pk, employee_id=emp.pk,
                                    user=self.owner, left_on=date(2000, 1, 1))
        with self.assertRaises(ValidationError):
            payroll.update_employee(business_id=self.business.pk, employee_id=emp.pk,
                                    user=self.owner, code="EMP-9")

    def test_codes_are_sequential_per_business(self):
        self.assertEqual([self.hire(n).code for n in "AB"], ["EMP-1", "EMP-2"])
        other = payroll.create_employee(
            business_id=self.other_business.pk, user=self.other_owner, name="X",
            monthly_salary="100", joined_on=date(2025, 1, 1))
        self.assertEqual(other.code, "EMP-1")

    def test_db_guards(self):
        emp = self.hire()
        self.generate(previous_month())
        kw = dict(business=self.business, employee=emp, kind="SALARY", amount=D("1"),
                  created_by=self.owner)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SalaryAccrual.objects.create(month=previous_month(), **kw)  # second live salary
        with self.assertRaises(IntegrityError), transaction.atomic():
            SalaryAccrual.objects.create(month=previous_month().replace(day=2), **kw)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SalaryAccrual.objects.create(month=NOV, **{**kw, "kind": "BONUS"})  # no reason

    def test_rows_are_immutable(self):
        emp = self.hire()
        acc = self.generate(previous_month())["created"][0]
        acc.amount = D("1")
        with self.assertRaises(ValueError):
            acc.save()
        with self.assertRaises(ValueError):
            SalaryAccrual.objects.all().update(amount=1)
        with self.assertRaises(ValueError):
            emp.delete()


class StatementTests(ExpBase):
    def test_statement_matches_live_position_and_as_of(self):
        emp = self.hire()
        pm = previous_month()
        self.generate(pm)
        pay = self.pay_emp(emp, "5000.00")
        self.adjust(emp, "DEDUCTION", pm, "800.00", reason="absent")
        payroll.cancel_salary_payment(business_id=self.business.pk, payment_id=pay.pk,
                                      user=self.owner, reason="reverted")
        today = timezone.localdate()
        st = employee_statement(emp, pm, today)
        self.assertEqual(st["opening_balance"], D("0.00"))
        self.assertEqual(st["closing_balance"], employee_position(emp)["balance"])
        self.assertEqual(st["closing_balance"], D("11200.00"))
        self.assertCountEqual([r["type"] for r in st["rows"]],
                              ["SALARY", "PAYMENT", "DEDUCTION", "PAYMENT_CANCEL"])
        self.assertEqual(st["rows"][-1]["balance"], st["closing_balance"])
        # as_of "now" equals the live figure
        far = timezone.now() + timedelta(days=1)
        self.assertEqual(employee_position(emp, as_of=far), employee_position(emp))

    def test_opening_balance_of_later_window(self):
        emp = self.hire()
        self.generate(previous_month())
        st = employee_statement(emp, timezone.localdate(), timezone.localdate())
        self.assertEqual((st["opening_balance"], st["closing_balance"], st["rows"]),
                         (D("12000.00"), D("12000.00"), []))

    def test_balances_one_pass(self):
        a, b = self.hire("A"), self.hire("B")
        self.generate(previous_month())
        self.pay_emp(a, "15000.00")
        self.assertEqual(employee_balances(self.business.pk),
                         {a.pk: D("-3000.00"), b.pk: D("12000.00")})
