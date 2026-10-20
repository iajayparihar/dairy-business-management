"""
Tenants, logins and memberships.

Username pattern: <firstname>_<role>_<business>, e.g. ajay_owner_abhigyan.
Every login uses the same password (base.PASSWORD).

Edge cases seeded on purpose:
- two OWNERS in one business (abhigyan);
- one user with roles in two businesses (manager in abhigyan, staff in vij);
- a former staff member with an inactive membership;
- a login that belongs to no business;
- an inactive (closed) business with its own owner.
"""
from types import SimpleNamespace

from django.contrib.auth import get_user_model

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import create_business

from .base import PASSWORD

User = get_user_model()

# key, name, city, bill language, milk volume scale, customers, farmers, routes, members
TENANTS = [
    {
        "key": "abhigyan", "name": "Abhigyan Dairy", "city": "Indore",
        "bill_language": "en", "scale": 1.0, "customers": 18, "farmers": 6,
        "routes": ["Vijay Nagar", "Palasia", "Rajwada"],
        "members": [
            ("ajay", "Ajay", "Sharma", "OWNER"),
            ("priya", "Priya", "Sharma", "OWNER"),
            ("suresh", "Suresh", "Patel", "MANAGER"),
            ("raju", "Raju", "Yadav", "STAFF"),
            ("mohan", "Mohan", "Verma", "STAFF"),
            ("deepak", "Deepak", "Kushwaha", "STAFF"),
        ],
    },
    {
        "key": "vij", "name": "Vij Dairy", "city": "Bhopal",
        "bill_language": "hi", "scale": 0.7, "customers": 12, "farmers": 4,
        "routes": ["Arera Colony", "MP Nagar"],
        "members": [
            ("vikram", "Vikram", "Vij", "OWNER"),
            ("kavita", "Kavita", "Mishra", "MANAGER"),
            ("sonu", "Sonu", "Ahirwar", "STAFF"),
            ("pappu", "Pappu", "Lodhi", "STAFF"),
        ],
    },
    {
        "key": "gopal", "name": "Gopal Dairy", "city": "Ujjain",
        "bill_language": "en", "scale": 0.4, "customers": 6, "farmers": 3,
        "routes": ["Freeganj"],
        "members": [
            ("gopal", "Gopal", "Joshi", "OWNER"),
            ("ramu", "Ramu", "Bairagi", "STAFF"),
        ],
    },
]

PHONES = iter(range(9826100001, 9826199999))


def _user(username, first, last, **extra):
    user = User.objects.create_user(
        username=username, password=PASSWORD,
        first_name=first, last_name=last,
        email=f"{username}@example.com", phone_number=str(next(PHONES)),
        **extra,
    )
    return user


def _member(business, user, role, is_active=True):
    return BusinessMembership.objects.create(
        business=business, user=user, role=role, is_active=is_active,
    )


def seed():
    """Returns {key: tenant namespace} plus the list of logins for the summary."""
    logins = [("admin", "Django superuser (admin site)", "-")]
    User.objects.create_superuser(
        username="admin", password=PASSWORD, email="admin@example.com",
    )

    tenants = {}
    for spec in TENANTS:
        key, users = spec["key"], {}
        for slug, first, last, role in spec["members"]:
            username = f"{slug}_{role.lower()}_{key}"
            users[slug] = (_user(username, first, last), role)
            logins.append((username, role, spec["name"]))

        owner = next(u for u, role in users.values() if role == "OWNER")
        business = create_business(
            name=spec["name"], slug=f"{key}-dairy", owner=owner,
            phone=str(next(PHONES)), email=f"contact@{key}dairy.example.com",
            address=f"12, Main Road, {spec['city']}, Madhya Pradesh",
            bill_language=spec["bill_language"],
        )
        memberships = {}
        for slug, (user, role) in users.items():
            memberships[slug] = (
                BusinessMembership.objects.get(business=business, user=user)
                if user == owner else _member(business, user, role)
            )

        by_role = {}
        for user, role in users.values():
            by_role.setdefault(role, []).append(user)
        tenants[key] = SimpleNamespace(
            key=key, spec=spec, business=business, scale=spec["scale"],
            owner=owner,
            manager=by_role.get("MANAGER", [owner])[0],
            staff=by_role.get("STAFF", []),
            staff_members=[m for m in memberships.values() if m.role == "STAFF"],
        )

    # One person, two businesses: manager in Abhigyan, staff in Vij.
    neha = _user("neha_manager_abhigyan_staff_vij", "Neha", "Rawat")
    _member(tenants["abhigyan"].business, neha, "MANAGER")
    _member(tenants["vij"].business, neha, "STAFF")
    logins.append((neha.username, "MANAGER + STAFF", "Abhigyan Dairy + Vij Dairy"))

    # Former staff: membership switched off, must be refused everywhere.
    vikas = _user("vikas_staff_abhigyan_inactive", "Vikas", "Chouhan")
    _member(tenants["abhigyan"].business, vikas, "STAFF", is_active=False)
    logins.append((vikas.username, "STAFF (inactive membership)", "Abhigyan Dairy"))

    # A login with no business at all.
    kiran = _user("kiran_no_business", "Kiran", "Solanki")
    logins.append((kiran.username, "no membership", "-"))

    # A closed tenant: business switched off.
    shyam = _user("shyam_owner_krishna_closed", "Shyam", "Goyal")
    closed = create_business(
        name="Shree Krishna Dairy (closed)", slug="krishna-dairy", owner=shyam,
        address="5, Station Road, Dewas, Madhya Pradesh",
    )
    Business.objects.filter(pk=closed.pk).update(is_active=False)
    logins.append((shyam.username, "OWNER (inactive business)", closed.name))

    return tenants, logins
