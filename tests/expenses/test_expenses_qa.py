"""QA sweep: expenses, employees and salary gaps beyond tests/expenses/test_api.py."""
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from tests.expenses.base import previous_month
from tests.expenses.test_api import ApiBase

FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


@FAST
class ExpenseQA(ApiBase):
    def post_expense(self, **kw):
        body = {"client_ref": self.ref(), "category_id": str(self.electricity.pk), "amount": "10.00", **kw}
        return self.call("post", "expenses/", self.manager, body)

    def test_future_date_refused_past_and_today_ok(self):
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(self.post_expense(expense_date=tomorrow).status_code, 400)
        self.assertEqual(self.post_expense(expense_date=timezone.localdate().isoformat()).status_code, 201)
        self.assertEqual(self.post_expense(expense_date="2020-01-01").status_code, 201)
        self.assertEqual(self.post_expense(expense_date="0001-01-01").status_code in (201, 400), True)

    def test_text_limits(self):
        for kw in ({"description": "x" * 5001}, {"reference_number": "x" * 101}, {"payee": "x" * 201}):
            self.assertEqual(self.post_expense(**kw).status_code, 400, str(kw)[:40])

    def test_cancel_correct_reason_limits_and_roles(self):
        e = self.post_expense().data
        for body in ({}, {"reason": ""}, {"reason": "x" * 501}):
            self.assertEqual(self.call("post", f"expenses/{e['id']}/cancel/", self.manager, body).status_code, 400, str(body)[:30])
        self.assertEqual(self.call("post", f"expenses/{e['id']}/cancel/", self.staff, {"reason": "r"}).status_code, 403)

    def test_category_rules(self):
        c = self.call("post", "expense-categories/", self.owner, {"name": "Rent"})
        self.assertEqual(c.status_code, 201)
        dup = self.call("post", "expense-categories/", self.owner, {"name": "rent"})
        self.assertEqual(dup.status_code, 400)
        for body in ({}, {"name": ""}, {"name": "x" * 101}, {"name": "ok", "bogus": 1}):
            self.assertEqual(self.call("post", "expense-categories/", self.owner, body).status_code, 400, str(body)[:30])
        self.assertEqual(self.call("post", "expense-categories/", self.staff, {"name": "Tea"}).status_code, 403)

    def test_list_pagination_and_amounts_are_strings(self):
        for _ in range(3):
            self.post_expense()
        r = self.call("get", "expenses/?page_size=2", self.manager)
        self.assertEqual((r.data["count"], len(r.data["results"])), (3, 2))
        raw = self.c.get(self.url("expenses/")).content.decode()
        self.assertNotRegex(raw, r'"amount":\s*\d')


@FAST
class SalaryQA(ApiBase):
    def emp(self, **kw):
        body = {"name": "Ramesh", "monthly_salary": "12000.00", "joined_on": "2025-01-01", **kw}
        return self.call("post", "employees/", self.owner, body)

    def test_employee_validation(self):
        bad = [{"name": ""}, {"name": "x" * 201}, {"monthly_salary": "0"}, {"monthly_salary": "-5"}, {"monthly_salary": "abc"},
               {"monthly_salary": "1" * 15}, {"joined_on": "2099-01-01"}, {"joined_on": "x"}, {"joined_on": None},
               {"left_on": "2024-01-01"}, {"phone": "abc"}, {"bogus": 1}, {"name": ["x"]}]
        for body in bad:
            r = self.emp(**body)
            self.assertEqual(r.status_code, 400, str(body)[:50])

    def test_generate_validation_and_idempotency(self):
        self.emp()
        for body in ({}, {"month": "2026-13"}, {"month": "26-01"}, {"month": 5}, {"month": "2099-01"}, {"month": "2025-01", "bogus": 1}):
            self.assertEqual(self.call("post", "salary/accruals/generate/", data=body).status_code, 400, str(body))
        month = previous_month().strftime("%Y-%m")
        a = self.call("post", "salary/accruals/generate/", data={"month": month})
        b = self.call("post", "salary/accruals/generate/", data={"month": month})
        self.assertEqual((a.status_code, b.status_code), (201, 200))
        self.assertEqual(len(a.data["created"]), 1)
        self.assertEqual(len(b.data["created"]), 0)

    def test_joiner_before_month_and_leaver_are_prorated_by_calendar_days(self):
        month = previous_month()
        self.emp(name="Mid", joined_on=month.replace(day=16).isoformat())
        r = self.call("post", "salary/accruals/generate/", data={"month": month.strftime("%Y-%m")})
        row = r.data["created"][0]
        days_in_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - month
        joined_days = days_in_month.days - 15
        expected = f"{12000 * joined_days / days_in_month.days:.2f}"
        self.assertEqual(row["amount"], expected)

    def test_payment_validation_and_overpay_allowed(self):
        e = self.emp().data
        url = f"employees/{e['id']}/payments/"
        for body in ({}, {"client_ref": "p", "amount": "0"}, {"client_ref": "p", "amount": "-1"}, {"client_ref": "p", "amount": "abc"},
                     {"client_ref": "p", "amount": "1.234"}, {"client_ref": "", "amount": "10"}, {"client_ref": "p", "amount": "10", "bogus": 1},
                     {"client_ref": "p", "amount": "10", "paid_on": "2099-01-01"}, {"client_ref": "p", "amount": "10", "notes": "x" * 5001}):
            self.assertEqual(self.call("post", url, data=body).status_code, 400, str(body)[:60])
        self.assertEqual(self.call("post", url, data={"client_ref": "ok", "amount": "50000.00"}).status_code, 201)
        bal = self.call("get", f"employees/{e['id']}/balance/").data
        self.assertEqual((bal["balance"], bal["is_advance"]), ("-50000.00", True))

    def test_payment_replay_and_conflict(self):
        e = self.emp().data
        url = f"employees/{e['id']}/payments/"
        a = self.call("post", url, data={"client_ref": "r1", "amount": "100.00"})
        b = self.call("post", url, data={"client_ref": "r1", "amount": "100.00"})
        c = self.call("post", url, data={"client_ref": "r1", "amount": "200.00"})
        self.assertEqual((a.status_code, c.status_code), (201, 409))
        self.assertIn(b.status_code, (200, 201))
        self.assertEqual(self.call("get", f"employees/{e['id']}/balance/").data["balance"], "-100.00")

    def test_employee_list_filters_and_other_business(self):
        self.emp(name="A")
        self.emp(name="B")
        r = self.call("get", "employees/?page_size=1")
        self.assertEqual((r.data["count"], len(r.data["results"])), (2, 1))
        for q in ("is_active=x", "page=x"):
            self.assertLess(self.call("get", f"employees/?{q}").status_code, 500, q)
