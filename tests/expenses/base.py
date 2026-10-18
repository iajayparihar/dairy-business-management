"""Fixtures for the Phase 8 tests (reuses the billing business and users)."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.expenses import payroll, services
from apps.expenses.models import Employee
from tests.billing.base import BillingBase

D = Decimal


def first_of_this_month():
    return timezone.localdate().replace(day=1)


def previous_month():
    return (first_of_this_month() - timedelta(days=1)).replace(day=1)


class ExpBase(BillingBase, TestCase):
    def setUp(self):
        self.build()
        self._ref = 0
        self.electricity = services.create_category(
            business_id=self.business.pk, user=self.owner, name="Electricity")

    def ref(self):
        self._ref += 1
        return f"ref-{self._ref}"

    def spend(self, amount="100.00", user=None, **kw):
        kw.setdefault("client_ref", self.ref())
        kw.setdefault("category_id", self.electricity.pk)
        return services.create_expense(
            business_id=self.business.pk, user=user or self.manager,
            amount=amount, **kw)

    def hire(self, name="Ramesh", salary="12000.00", joined=date(2025, 1, 1), **kw):
        return payroll.create_employee(
            business_id=self.business.pk, user=self.owner, name=name,
            monthly_salary=salary, joined_on=joined, **kw)

    def generate(self, month, **kw):
        return payroll.generate_salary(
            business_id=self.business.pk, user=self.owner, month=month, **kw)

    def pay_emp(self, emp, amount, **kw):
        kw.setdefault("client_ref", self.ref())
        return payroll.create_salary_payment(
            business_id=self.business.pk, user=self.owner,
            employee_id=emp.pk, amount=amount, **kw)

    def adjust(self, emp, kind, month, amount, reason="festival", **kw):
        kw.setdefault("client_ref", self.ref())
        return payroll.record_adjustment(
            business_id=self.business.pk, user=self.owner, employee_id=emp.pk,
            kind=kind, month=month, amount=amount, reason=reason, **kw)
