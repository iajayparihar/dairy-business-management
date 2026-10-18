from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from apps.expenses import payroll
from tests.expenses.base import ExpBase, previous_month


class ApiBase(ExpBase):
    def setUp(self):
        super().setUp()
        self.c = APIClient()

    def url(self, path, business=None):
        return f"/api/v1/businesses/{(business or self.business).pk}/{path}"

    def call(self, method, path, user=None, data=None, business=None):
        self.c.force_authenticate(user or self.owner)
        kw = {"format": "json"} if data is not None else {}
        return getattr(self.c, method)(self.url(path, business), data, **kw)


class ExpenseApiTests(ApiBase):
    def post_expense(self, **kw):
        body = {"client_ref": self.ref(), "category_id": str(self.electricity.pk),
                "amount": "15000.00", **kw}
        return self.call("post", "expenses/", self.manager, body)

    def test_create_list_filter_detail(self):
        r = self.post_expense(payee="MSEB", payment_method="UPI")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["amount"], r.data["payee"], r.data["category_name"]),
                         ("15000.00", "MSEB", "Electricity"))
        self.post_expense(amount="10.00")
        lst = self.call("get", "expenses/?payment_method=UPI", self.manager)
        self.assertEqual(lst.data["count"], 1)
        today = timezone.localdate().isoformat()
        self.assertEqual(self.call("get", f"expenses/?from={today}&to={today}").data["count"], 2)
        self.assertEqual(self.call("get", "expenses/?search=mseb").data["count"], 1)
        self.assertEqual(self.call("get", f"expenses/{r.data['id']}/").status_code, 200)

    def test_staff_forbidden_and_anonymous(self):
        for method, path in (("get", "expenses/"), ("post", "expenses/"),
                             ("get", "expense-categories/"), ("get", "expenses/summary/")):
            self.assertEqual(self.call(method, path, self.staff, {} if method == "post" else None).status_code, 403)
        self.c.force_authenticate(None)
        self.assertEqual(self.c.get(self.url("expenses/")).status_code, 401)

    def test_unknown_and_forged_fields_rejected(self):
        for extra in ({"status": "CANCELLED"}, {"expense_number": "EXP-1"}, {"junk": 1}):
            r = self.post_expense(**extra)
            self.assertEqual(r.status_code, 400, extra)
        r = self.call("post", "expenses/", self.manager, {"amount": "1"})
        self.assertEqual(r.status_code, 400)

    def test_garbage_input_is_400_never_500(self):
        bad_bodies = [
            {"amount": "abc"}, {"amount": "NaN"}, {"amount": "1e30"}, {"amount": "1.234"},
            {"amount": 0}, {"amount": None}, {"amount": ["1"]},
            {"expense_date": "2026-02-31"}, {"expense_date": "9999-12-31"},
            {"expense_date": 5}, {"payment_method": "GOLD"}, {"payee": 5},
            {"payee": "x" * 201}, {"category_id": "nope"}, {"category_id": None},
            {"client_ref": "x" * 65}, {"client_ref": ""},
        ]
        for bad in bad_bodies:
            with self.subTest(bad):
                self.assertEqual(self.post_expense(**bad).status_code, 400)
        self.assertEqual(self.call("post", "expenses/", self.manager, ["x"]).status_code, 400)
        for q in ("from=2026-13-01", "to=abc", "status=NOPE", "category_id=zzz",
                  "payment_method=X", "from=2026-05-02&to=2026-05-01"):
            with self.subTest(q):
                self.assertEqual(self.call("get", f"expenses/?{q}").status_code, 400)
        for q in ("from=2026-01-01", "from=2020-01-01&to=2026-01-01", "to=2026-01-01"):
            self.assertEqual(self.call("get", f"expenses/summary/?{q}").status_code, 400)

    def test_nul_byte_is_400(self):
        self.assertEqual(self.post_expense(payee="a\x00b").status_code, 400)
        self.assertEqual(self.call("get", "expenses/?search=a%00b").status_code, 400)

    def test_cancel_and_correct(self):
        e = self.post_expense().data
        bad = self.call("post", f"expenses/{e['id']}/cancel/", self.manager, {"reason": "x"})
        self.assertEqual(bad.status_code, 400)
        fixed = self.call("post", f"expenses/{e['id']}/correct/", self.manager,
                          {"reason": "typo", "amount": "15500.00"})
        self.assertEqual(fixed.status_code, 201, fixed.data)
        self.assertEqual((fixed.data["amount"], fixed.data["replaces"]), ("15500.00", e["id"]))
        old = self.call("get", f"expenses/{e['id']}/").data
        self.assertEqual((old["status"], old["replaced_by"]), ("CANCELLED", fixed.data["id"]))
        for body in ({"reason": "typo", "expense_date": "2026-01-01"},
                     {"reason": "typo", "client_ref": "z", "amount": "1"}, {"reason": "typo"}):
            r = self.call("post", f"expenses/{fixed.data['id']}/correct/", self.manager, body)
            self.assertEqual(r.status_code, 400, body)

    def test_replay_and_conflict_codes(self):
        body = {"client_ref": "same", "category_id": str(self.electricity.pk), "amount": "5.00"}
        a = self.call("post", "expenses/", self.manager, body)
        b = self.call("post", "expenses/", self.manager, body)
        self.assertEqual((a.status_code, b.status_code, a.data["id"]), (201, 201, b.data["id"]))
        c = self.call("post", "expenses/", self.manager, {**body, "amount": "6.00"})
        self.assertEqual(c.status_code, 409)

    def test_other_business_is_404_or_403(self):
        e = self.post_expense().data
        # a different business's owner cannot see this expense under their own business
        r = self.call("get", f"expenses/{e['id']}/", self.other_owner, business=self.other_business)
        self.assertEqual(r.status_code, 404)
        r = self.call("get", f"expenses/{e['id']}/", self.other_owner)
        self.assertEqual(r.status_code, 403)

    def test_categories_api(self):
        r = self.call("post", "expense-categories/", self.manager, {"name": "Rent"})
        self.assertEqual(r.status_code, 201)
        dup = self.call("post", "expense-categories/", self.manager, {"name": "rent"})
        self.assertEqual(dup.status_code, 400)
        off = self.call("patch", f"expense-categories/{r.data['id']}/", self.manager,
                        {"is_active": False})
        self.assertFalse(off.data["is_active"])
        self.assertEqual(self.call("patch", f"expense-categories/{r.data['id']}/",
                                   self.manager, {"is_active": "no"}).status_code, 400)
        self.assertEqual(self.call("patch", f"expense-categories/{r.data['id']}/",
                                   self.manager, {"junk": 1}).status_code, 400)
        self.assertEqual(self.call("get", "expense-categories/?is_active=false").data["count"], 1)

    def test_summary(self):
        self.post_expense(amount="100.00")
        e = self.post_expense(amount="50.00").data
        self.call("post", f"expenses/{e['id']}/cancel/", self.manager, {"reason": "oops"})
        r = self.call("get", "expenses/summary/")
        self.assertEqual((r.status_code, r.data["total"]), (200, "100.00"))
        self.assertEqual(r.data["categories"][0]["count"], 1)


class SalaryApiTests(ApiBase):
    def create_emp(self, **kw):
        body = {"name": "Ramesh", "monthly_salary": "12000.00", "joined_on": "2025-01-01", **kw}
        return self.call("post", "employees/", self.owner, body)

    def test_manager_and_staff_forbidden_everywhere(self):
        emp = self.hire()
        paths = [("get", "employees/"), ("post", "employees/"),
                 ("get", f"employees/{emp.pk}/"), ("patch", f"employees/{emp.pk}/"),
                 ("get", f"employees/{emp.pk}/balance/"), ("get", f"employees/{emp.pk}/statement/"),
                 ("get", f"employees/{emp.pk}/payments/"), ("post", f"employees/{emp.pk}/payments/"),
                 ("post", f"employees/{emp.pk}/adjustments/"), ("get", "salary/accruals/"),
                 ("post", "salary/accruals/generate/")]
        for user in (self.manager, self.staff):
            for method, path in paths:
                with self.subTest(user=user.username, path=path):
                    r = self.call(method, path, user, {} if method != "get" else None)
                    self.assertEqual(r.status_code, 403)

    def test_full_example_over_http(self):
        emp = self.create_emp().data
        self.assertEqual((emp["code"], emp["balance"]), ("EMP-1", "0.00"))
        month = previous_month().strftime("%Y-%m")
        gen = self.call("post", "salary/accruals/generate/", data={"month": month})
        self.assertEqual((gen.status_code, gen.data["created"][0]["amount"]), (201, "12000.00"))
        again = self.call("post", "salary/accruals/generate/", data={"month": month})
        self.assertEqual((again.status_code, again.data["skipped"][0]["reason"]),
                         (200, "Already generated."))
        pay = self.call("post", f"employees/{emp['id']}/payments/",
                        data={"client_ref": "p1", "amount": "15000.00"})
        self.assertEqual(pay.status_code, 201, pay.data)
        bal = self.call("get", f"employees/{emp['id']}/balance/").data
        self.assertEqual((bal["balance"], bal["is_advance"]), ("-3000.00", True))
        listed = self.call("get", "employees/").data["results"][0]
        self.assertEqual((listed["balance"], listed["is_advance"]), ("-3000.00", True))
        st = self.call("get", f"employees/{emp['id']}/statement/?from={previous_month()}&to={timezone.localdate()}").data
        self.assertEqual((st["closing_balance"], [t["type"] for t in st["transactions"]]),
                         ("-3000.00", ["SALARY", "PAYMENT"]))
        cancel = self.call("post", f"salary-payments/{pay.data['id']}/cancel/", data={"reason": "wrong person"})
        self.assertEqual(cancel.data["status"], "CANCELLED")
        self.assertEqual(self.call("get", f"employees/{emp['id']}/balance/").data["balance"], "12000.00")

    def test_adjustment_endpoint_and_accrual_list_cancel(self):
        emp = self.create_emp().data
        month = previous_month().strftime("%Y-%m")
        r = self.call("post", f"employees/{emp['id']}/adjustments/", data={
            "client_ref": "a1", "kind": "BONUS", "month": month, "amount": "2000.00", "reason": "Diwali"})
        self.assertEqual((r.status_code, r.data["kind"]), (201, "BONUS"))
        lst = self.call("get", f"salary/accruals/?month={month}&kind=BONUS&status=POSTED").data
        self.assertEqual(lst["count"], 1)
        c = self.call("post", f"salary/accruals/{r.data['id']}/cancel/", data={"reason": "mistake"})
        self.assertEqual(c.data["status"], "CANCELLED")
        for body in ({"kind": "SALARY"}, {"month": "2026-13"}, {"reason": ""}, {"amount": "-1"}, {"junk": 1}):
            full = {"client_ref": "a2", "kind": "BONUS", "month": month, "amount": "1.00",
                    "reason": "ok", **body}
            self.assertEqual(self.call("post", f"employees/{emp['id']}/adjustments/", data=full).status_code,
                             400, body)

    def test_garbage_is_400(self):
        emp = self.create_emp().data
        for body in ({"name": ""}, {"monthly_salary": "0"}, {"monthly_salary": "x"},
                     {"joined_on": "2025-02-30"}, {"joined_on": None}, {"code": "EMP-9"},
                     {"name": "a\x00"}, {"phone": "9" * 21}):
            with self.subTest(body):
                self.assertEqual(self.create_emp(**body).status_code, 400)
        for body in ({"month": "2026-13"}, {"month": "2026-5"}, {"month": 5}, {"month": "2999-01"},
                     {"month": "2026-01", "employee_ids": []}, {"month": "2026-01", "employee_ids": "x"},
                     {"month": "2026-01", "employee_ids": ["bad"]}, {"month": "2026-01", "overrides": []}, {}):
            with self.subTest(body):
                self.assertEqual(self.call("post", "salary/accruals/generate/", data=body).status_code, 400)
        for body in ({"amount": "0"}, {"paid_at": "2026-02-31T10:00:00+05:30"}, {"paid_at": "2026-01-01T10:00:00"},
                     {"payment_method": "GOLD"}, {"payment_number": "X"}):
            full = {"client_ref": "z", "amount": "1.00", **body}
            with self.subTest(body):
                self.assertEqual(self.call("post", f"employees/{emp['id']}/payments/", data=full).status_code, 400)
        for q in ("from=2026-01-01", "from=2020-01-01&to=2026-01-01", "to=bad"):
            with self.subTest(q):
                self.assertEqual(self.call("get", f"employees/{emp['id']}/statement/?{q}").status_code, 400)
        for q in ("month=2026-13", "kind=X", "status=X", "employee_id=zzz"):
            with self.subTest(q):
                self.assertEqual(self.call("get", f"salary/accruals/?{q}").status_code, 400)

    def test_patch_employee(self):
        emp = self.create_emp().data
        r = self.call("patch", f"employees/{emp['id']}/", data={"monthly_salary": "13000.00", "designation": "Driver"})
        self.assertEqual((r.status_code, r.data["monthly_salary"]), (200, "13000.00"))
        for body in ({"code": "X"}, {"is_active": "no"}, {"left_on": "2000-01-01"}, {"monthly_salary": "-1"}):
            self.assertEqual(self.call("patch", f"employees/{emp['id']}/", data=body).status_code, 400, body)
        # read-only sub-resources do not accept PATCH / POST
        self.assertEqual(self.call("patch", f"employees/{emp['id']}/balance/", data={}).status_code, 405)

    def test_cross_tenant_employee_is_404(self):
        other = payroll.create_employee(
            business_id=self.other_business.pk, user=self.other_owner, name="X",
            monthly_salary="100", joined_on=timezone.localdate() - timedelta(days=400))
        for method, path in (("get", f"employees/{other.pk}/"), ("get", f"employees/{other.pk}/balance/"),
                             ("get", f"employees/{other.pk}/payments/")):
            self.assertEqual(self.call(method, path).status_code, 404)
        r = self.call("post", f"employees/{other.pk}/payments/", data={"client_ref": "q", "amount": "1"})
        self.assertEqual(r.status_code, 404)
