"""
Expenses and payroll.

Expenses (manager): rent on the 1st, electricity on the 10th, van diesel
every 3 days, packaging weekly, small tea / cleaning spends. One duplicate
is cancelled and one amount is corrected.

Payroll (owner only): employees (one joined mid last month -> pro-rata, one
left last month), last month's salary generated, a festival bonus and a
leave deduction, salaries paid early this month (one only partly), and one
advance this month (negative balance = advance).
"""
from datetime import timedelta

from apps.expenses.models import Employee
from apps.expenses.payroll import (
    create_employee,
    create_salary_payment,
    generate_salary,
    record_adjustment,
    update_employee,
)
from apps.expenses.selectors import employee_position
from apps.expenses.services import cancel_expense, correct_expense, create_category, create_expense

from .base import at, history_days, history_start, is_past, money, rng, this_month, today

CATEGORIES = ["Shop Rent", "Electricity", "Vehicle Diesel", "Packaging Material",
              "Tea & Snacks", "Cleaning Supplies", "Vehicle Repair"]

# name, designation, monthly salary at full scale
EMPLOYEES = [
    ("Raju Yadav", "Delivery Boy", 9000),
    ("Mohan Verma", "Delivery Boy", 9000),
    ("Laxmi Bai", "Dairy Helper", 8000),
    ("Suresh Patel", "Shop Manager", 18000),
    ("Rohit Malviya", "Paneer Maker", 11000),  # joins mid last month
    ("Sanjay Thakur", "Helper", 7500),         # leaves last month
]


def _expenses(t, cats):
    biz, n = t.business, 0
    for day in history_days():
        plan = []
        if day.day == 1:
            plan.append(("Shop Rent", 12000, "BANK_TRANSFER", "Kailash Gupta (landlord)"))
        if day.day == 10:
            plan.append(("Electricity", rng.randint(2800, 3600), "UPI", "Electricity board"))
        if day.toordinal() % 3 == 0:
            plan.append(("Vehicle Diesel", rng.randint(700, 1200), "CASH", "Petrol pump, Ring Road"))
        if day.weekday() == 0:
            plan.append(("Packaging Material", rng.randint(500, 900), "UPI", "Shree Packaging"))
        if rng.random() < 0.3:
            plan.append(("Tea & Snacks", rng.randint(60, 180), "CASH", "Tea stall"))
        if rng.random() < 0.1:
            plan.append(("Cleaning Supplies", rng.randint(150, 400), "CASH", "General store"))
        for cat, amount, method, payee in plan:
            n += 1
            create_expense(
                business_id=biz.pk, user=t.manager, client_ref=f"seed-exp-{n}",
                category_id=cats[cat].pk, amount=money(round(amount * max(t.scale, 0.5))),
                expense_date=day, payment_method=method, payee=payee,
                description=f"{cat} - {day:%d %b}",
            )

    repair = create_expense(
        business_id=biz.pk, user=t.manager, client_ref="seed-exp-repair",
        category_id=cats["Vehicle Repair"].pk, amount=money(2500),
        expense_date=today() - timedelta(days=6), payment_method="CASH",
        payee="Sharma Auto Works", description="Van clutch plate",
    )
    duplicate = create_expense(
        business_id=biz.pk, user=t.manager, client_ref="seed-exp-repair-dup",
        category_id=cats["Vehicle Repair"].pk, amount=money(2500),
        expense_date=today() - timedelta(days=6), payment_method="CASH",
        payee="Sharma Auto Works", description="Van clutch plate",
    )
    cancel_expense(business_id=biz.pk, expense_id=duplicate.pk, user=t.owner,
                   reason="Same bill entered twice.")
    correct_expense(business_id=biz.pk, expense_id=repair.pk, user=t.owner,
                    reason="Final bill was Rs. 2,850 with labour.", amount=money(2850))


def _payroll(t):
    biz, owner = t.business, t.owner
    last_month, first = history_start(), this_month()
    staff = []
    for i, (name, role, salary) in enumerate(EMPLOYEES):
        joined = last_month + timedelta(days=14) if role == "Paneer Maker" else last_month - timedelta(days=200 + i * 40)
        staff.append(create_employee(
            business_id=biz.pk, user=owner, name=name, designation=role,
            monthly_salary=money(round(salary * max(t.scale, 0.6), -2)),
            joined_on=joined, phone=f"70000{i:05d}",
        ))
    leaver = staff[-1]
    update_employee(business_id=biz.pk, employee_id=leaver.pk, user=owner,
                    left_on=last_month + timedelta(days=19), is_active=False)

    generate_salary(business_id=biz.pk, user=owner, month=last_month)
    record_adjustment(
        business_id=biz.pk, user=owner, employee_id=staff[0].pk, client_ref="seed-bonus-1",
        kind="BONUS", month=last_month, amount=money(1000), reason="Festival bonus (Navratri).",
    )
    record_adjustment(
        business_id=biz.pk, user=owner, employee_id=staff[1].pk, client_ref="seed-deduct-1",
        kind="DEDUCTION", month=last_month, amount=money(600), reason="2 days unpaid leave.",
    )

    paid_at = at(first + timedelta(days=2), 18)
    for i, emp in enumerate(Employee.objects.filter(business=biz).order_by("name")):
        balance = employee_position(emp)["balance"]
        if balance <= 0 or not is_past(paid_at):
            continue
        amount = balance if i != 1 else money(round(balance / 2, -2))  # one paid in part
        create_salary_payment(
            business_id=biz.pk, user=owner, employee_id=emp.pk, client_ref=f"seed-sal-{i}",
            amount=amount, payment_method="CASH" if i % 2 else "UPI", paid_at=paid_at,
            notes=f"Salary {last_month:%B %Y}",
        )
    advance_at = at(today() - timedelta(days=2), 17)
    if advance_at > paid_at:
        create_salary_payment(
            business_id=biz.pk, user=owner, employee_id=staff[0].pk, client_ref="seed-sal-advance",
            amount=money(2000), payment_method="CASH", paid_at=advance_at,
            notes="Advance: family function.",
        )


def seed(t):
    cats = {
        name: create_category(business_id=t.business.pk, user=t.owner, name=name)
        for name in CATEGORIES
    }
    _expenses(t, cats)
    _payroll(t)
