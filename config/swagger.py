"""
Swagger grouping: drf-spectacular tags every operation with its URL prefix
("v1"). This hook replaces that with a domain tag, taken from the path, so
Swagger UI shows one collapsible section per area.

Rules are checked top to bottom; the first match wins, so the specific
patterns (a supplier's payments) must come before the general ones
(suppliers).
"""
import re

B = r"^/api/v1/businesses/\{business_id\}/"

# (tag, description, [path regexes]) in display order.
GROUPS = [
    ("1. Auth", "Login, token refresh, password reset and change.",
     [r"^/api/v1/auth/"]),
    ("2. Products & Prices", "Categories, products and effective-dated selling / purchase prices.",
     [B + r"(categories|products)/"]),
    ("3. Customers", "Customer master data.",
     [B + r"customers/(\{id\}/)?$"]),
    ("4. Suppliers & Milk Rates", "Supplier master data and per-supplier milk rates.",
     [B + r"suppliers/(\{id\}/|\{supplier_id\}/milk-rates/)?$"]),
    ("5. Milk Collection", "Daily can-by-can milk collection, cancel / correct, milk-to-stock mapping.",
     [B + r"milk-(collections|config)/", B + r"suppliers/\{supplier_id\}/milk-collections/"]),
    ("6. Purchases (non-milk)", "Packaged-goods purchases: draft, receive, cancel, returns, corrections.",
     [B + r"purchases/"]),
    ("7. Supplier Payments", "Pay suppliers, allocate, reverse; balance, statement and pay summary.",
     [B + r"payments/", B + r"suppliers/\{supplier_id\}/(balance|pay-summary|payments|statement)/"]),
    ("8. Routes & Deliveries", "Delivery routes, standing orders, daily route runs and deliveries.",
     [B + r"(routes|route-runs|standing-orders|deliveries)/"]),
    ("9. Shop Sales", "Counter sales (cash / online / credit), cancel, correct, daily summary.",
     [B + r"sales/"]),
    ("10. Customer Bills", "Generate, finalize, void and print (PDF) customer bills.",
     [B + r"bills/"]),
    ("11. Customer Payments", "Customer payments, allocation, reversal, balance, statement, opening balance.",
     [B + r"customer-payments/", B + r"customers/\{customer_id\}/"]),
    ("12. Stock & Production", "Stock balances, wastage / adjustments and production batches.",
     [B + r"stock/"]),
    ("13. Expenses", "Expense categories and expenses (owner / manager).",
     [B + r"expense(s|-categories)/"]),
    ("14. Employees & Salary", "Employees, salary accruals, bonus / deduction and salary payments (owner only).",
     [B + r"(employees|salary|salary-payments)/"]),
    ("15. Reports & Dashboard", "Dashboard, profit, ledgers, balances and CSV / PDF exports.",
     [B + r"reports/"]),
]

_RULES = [(tag, re.compile(rx)) for tag, _, rxs in GROUPS for rx in rxs]
TAGS = [{"name": tag, "description": desc} for tag, desc, _ in GROUPS]


def tag_for(path):
    for tag, rx in _RULES:
        if rx.search(path):
            return tag
    return "99. Other"


def group_by_domain(result, generator, request, public):
    for path, methods in result["paths"].items():
        for method, operation in methods.items():
            if isinstance(operation, dict) and "tags" in operation:
                operation["tags"] = [tag_for(path)]
    result["tags"] = TAGS
    return result
