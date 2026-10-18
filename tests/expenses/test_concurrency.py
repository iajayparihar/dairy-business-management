"""Real-thread races: business (no_key) -> employee (no_key) -> row."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from django.utils import timezone

from apps.expenses import payroll, services
from apps.expenses.models import Expense, SalaryAccrual, SalaryPayment
from apps.expenses.selectors import employee_position
from tests.billing.base import D
from tests.billing.test_concurrency import RaceBase, in_thread


def previous_month():
    return (timezone.localdate().replace(day=1) - timedelta(days=1)).replace(day=1)


class PayrollRace(RaceBase):
    def setUp(self):
        super().setUp()
        owner = self.owner_()
        self.cat = services.create_category(
            business_id=self.business.pk, user=owner, name="Electricity")
        self.emp = payroll.create_employee(
            business_id=self.business.pk, user=owner, name="Ramesh",
            monthly_salary="12000.00", joined_on=date(2020, 1, 1))

    def run_parallel(self, *fns, timeout=60):
        with ThreadPoolExecutor(max_workers=len(fns)) as pool:
            futures = [pool.submit(in_thread, fn) for fn in fns]
            return [f.result(timeout=timeout) for f in futures]

    def spend(self, ref):
        return lambda: services.create_expense(
            business_id=self.business.pk, user=self.owner_(), client_ref=ref,
            category_id=self.cat.pk, amount="10.00")

    def pay(self, ref, amount="100.00"):
        return lambda: payroll.create_salary_payment(
            business_id=self.business.pk, user=self.owner_(),
            employee_id=self.emp.pk, client_ref=ref, amount=amount)

    def generate(self):
        return lambda: payroll.generate_salary(
            business_id=self.business.pk, user=self.owner_(), month=previous_month())

    def test_parallel_expenses_get_distinct_numbers(self):
        results = self.run_parallel(*[self.spend(f"e{i}") for i in range(6)])
        self.assertEqual([r[0] for r in results], ["ok"] * 6)
        numbers = list(Expense.objects.values_list("expense_number", flat=True))
        self.assertEqual(len(set(numbers)), 6)

    def test_same_client_ref_makes_one_expense(self):
        results = self.run_parallel(*[self.spend("same") for _ in range(4)])
        self.assertEqual([r[0] for r in results], ["ok"] * 4)
        self.assertEqual(Expense.objects.count(), 1)

    def test_cancel_and_correct_race_has_one_winner(self):
        e = self.spend("x")()
        cancel = lambda: services.cancel_expense(
            business_id=self.business.pk, expense_id=e.pk, user=self.owner_(), reason="mistake")
        correct = lambda: services.correct_expense(
            business_id=self.business.pk, expense_id=e.pk, user=self.owner_(),
            reason="typo", amount="20.00")
        results = self.run_parallel(cancel, correct)
        self.assertEqual(sorted(r[0] for r in results), ["ok", "rejected"])
        self.assertEqual(Expense.objects.filter(status="POSTED").count() <= 1, True)

    def test_parallel_generate_makes_one_salary(self):
        results = self.run_parallel(*[self.generate() for _ in range(4)])
        self.assertEqual([r[0] for r in results], ["ok"] * 4)
        self.assertEqual(sum(len(r[1]["created"]) for r in results), 1)
        self.assertEqual(SalaryAccrual.objects.filter(kind="SALARY").count(), 1)

    def test_same_ref_payment_in_parallel_pays_once(self):
        results = self.run_parallel(*[self.pay("same") for _ in range(4)])
        self.assertEqual([r[0] for r in results], ["ok"] * 4)
        self.assertEqual(SalaryPayment.objects.count(), 1)

    def test_parallel_payments_unique_numbers_and_exact_balance(self):
        results = self.run_parallel(*[self.pay(f"p{i}") for i in range(6)])
        self.assertEqual([r[0] for r in results], ["ok"] * 6)
        numbers = list(SalaryPayment.objects.values_list("payment_number", flat=True))
        self.assertEqual(len(set(numbers)), 6)
        self.assertEqual(employee_position(self.emp)["balance"], D("-600.00"))

    def test_double_cancel_of_a_payment_cancels_once(self):
        pay = self.pay("a")()
        cancel = lambda: payroll.cancel_salary_payment(
            business_id=self.business.pk, payment_id=pay.pk, user=self.owner_(), reason="mistake")
        results = self.run_parallel(cancel, cancel)
        self.assertEqual(sorted(r[0] for r in results), ["ok", "rejected"])
        self.assertEqual(employee_position(self.emp)["balance"], D("0.00"))

    def test_mixed_writers_keep_the_balance_equal_to_the_event_sum(self):
        acc = self.generate()()["created"][0]
        first = self.pay("m0")()
        fns = [
            self.pay("m1", "500.00"), self.pay("m2", "700.00"), self.generate(),
            lambda: payroll.cancel_accrual(
                business_id=self.business.pk, accrual_id=acc.pk, user=self.owner_(), reason="dup"),
            lambda: payroll.cancel_salary_payment(
                business_id=self.business.pk, payment_id=first.pk, user=self.owner_(), reason="oops"),
            lambda: payroll.record_adjustment(
                business_id=self.business.pk, user=self.owner_(), employee_id=self.emp.pk,
                client_ref="adj", kind="BONUS", month=previous_month(), amount="50.00", reason="gift"),
        ]
        results = self.run_parallel(*fns)
        self.assertNotIn("error", [r[0] for r in results])
        live = employee_position(self.emp)
        replayed = employee_position(self.emp, as_of=timezone.now() + timedelta(days=1))
        self.assertEqual(live, replayed)
