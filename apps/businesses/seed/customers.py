"""
Customers, delivery routes (assigned to staff) and standing orders.

Each tenant takes the first N customers below (same codes C001.. across
tenants on purpose). Two extra customers per tenant: a kirana store that is
on no route and buys on credit at the counter, and a customer who moved away
(inactive).
"""
from apps.customers.models import Customer
from apps.routes.services import create_route, set_route_customers, set_standing_order

H, S = "HOUSEHOLD", "SHOP"
M, E = "MORNING", "EVENING"

# name, type, area, standing orders [(sku, quantity, shift)]
CUSTOMERS = [
    ("Rajesh Agrawal", H, "Scheme 54", [("COW-MILK", "2", M)]),
    ("Sunita Joshi", H, "Saket Nagar", [("BUF-MILK", "1", M), ("BUF-MILK", "1", E)]),
    ("Gupta Sweets", S, "Sarafa Bazaar", [("BUF-MILK", "10", M), ("BUF-MILK", "6", E)]),
    ("Anil Mehta", H, "Old Palasia", [("COW-MILK", "1.5", M)]),
    ("Kavita Dubey", H, "Sudama Nagar", [("COW-MILK", "1", M), ("DAHI", "0.5", M)]),
    ("Sharma Tea Stall", S, "Bus Stand", [("COW-MILK", "5", M), ("COW-MILK", "4", E)]),
    ("Pooja Tiwari", H, "Annapurna Road", [("BUF-MILK", "2", M)]),
    ("Manoj Saxena", H, "Bhawarkua", [("COW-MILK", "1", M), ("COW-MILK", "1", E)]),
    ("Annapurna Restaurant", S, "MG Road", [("COW-MILK", "8", M), ("DAHI", "3", M), ("PANEER", "1", E)]),
    ("Rekha Choudhary", H, "Rajendra Nagar", [("COW-MILK", "1.5", M)]),
    ("Vivek Jain", H, "Tilak Nagar", [("BUF-MILK", "1.5", M)]),
    ("Neelam Srivastava", H, "Manorama Ganj", [("COW-MILK", "2", M)]),
    ("Hotel Shree Palace", S, "Railway Station", [("BUF-MILK", "8", M), ("PANEER", "2", M)]),
    ("Arvind Bansal", H, "South Tukoganj", [("COW-MILK", "1", M)]),
    ("Meena Rathore", H, "Nanda Nagar", [("BUF-MILK", "1", M), ("DAHI", "1", M)]),
    ("Sandeep Khandelwal", H, "Khajrana", [("COW-MILK", "2", E)]),
    ("Asha Pandey", H, "Navlakha", [("COW-MILK", "1", M)]),
    ("Dinesh Soni", H, "Malharganj", [("BUF-MILK", "2", M)]),
]


def seed(t):
    biz, city = t.business, t.spec["city"]
    t.customers = {}   # code -> Customer
    t.orders = {}      # customer pk -> {shift: [(product, quantity)]}
    rows = CUSTOMERS[: t.spec["customers"]]
    rows += [
        ("Ravi Kirana Store", S, "Station Road", []),
        ("Prakash Nair (moved away)", H, "Lasudia", []),
    ]
    for i, (name, kind, area, orders) in enumerate(rows, start=1):
        code = f"C{i:03d}"
        t.customers[code] = Customer.objects.create(
            business=biz, customer_code=code, name=name, customer_type=kind,
            phone=f"98930{i:05d}", address=f"{10 + i * 7}, {area}, {city}",
        )
        for sku, qty, shift in orders:
            set_standing_order(
                business_id=biz.pk, user=t.manager, customer_id=t.customers[code].pk,
                product_id=t.products[sku].pk, quantity=qty, shift=shift,
            )
            t.orders.setdefault(t.customers[code].pk, {}).setdefault(shift, []).append(
                (t.products[sku], qty)
            )

    t.credit_customer = t.customers[f"C{len(rows) - 1:03d}"]
    gone = t.customers[f"C{len(rows):03d}"]
    gone.is_active = False
    gone.notes = "Moved to Pune in last month. Do not deliver."
    gone.save(update_fields=["is_active", "notes", "updated_at"])

    # Route customers round-robin; each route belongs to one staff member.
    on_route = [c for c in t.customers.values() if c.pk in t.orders]
    t.routes = []
    for r, area in enumerate(t.spec["routes"]):
        staff = t.staff_members[r % len(t.staff_members)]
        route = create_route(
            business_id=biz.pk, user=t.manager, name=f"{area} Route",
            assigned_to=staff.pk,
        )
        set_route_customers(
            business_id=biz.pk, route_id=route.pk, user=t.manager,
            customer_ids=[c.pk for c in on_route[r :: len(t.spec["routes"])]],
        )
        t.routes.append(route)
