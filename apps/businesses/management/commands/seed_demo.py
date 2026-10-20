import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.businesses.models import Business
from apps.businesses.seed import (
    billing,
    catalog,
    customers,
    deliveries,
    expenses,
    milk,
    production,
    purchases,
    sales,
    supplier_payments,
    suppliers,
    tenants,
)
from apps.businesses.seed.base import PASSWORD, history_start, today

# Order matters: suppliers are sized from customers' orders, stock comes in (milk, purchases, production) before it goes
# out, and bills are made only after every delivery / credit sale exists.
STEPS = [
    ("catalog", catalog), ("customers", customers), ("suppliers", suppliers),
    ("milk collections", milk), ("purchases", purchases),
    ("production + adjustments", production), ("route runs + deliveries", deliveries),
    ("shop sales", sales), ("bills + customer payments", billing),
    ("supplier payments", supplier_payments), ("expenses + payroll", expenses),
]


class Command(BaseCommand):
    help = "Load realistic multi-tenant demo data (DEBUG only). --fresh wipes the database first."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fresh", action="store_true",
            help="Flush the WHOLE database (all rows, including your own users) before seeding.",
        )

    def handle(self, *args, fresh=False, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo runs only with DEBUG=True (never on production data).")
        if fresh:
            call_command("flush", interactive=False, verbosity=0)
        elif Business.all_objects.filter(slug="abhigyan-dairy").exists():
            raise CommandError("Demo data is already loaded. Re-run with --fresh to wipe and reload.")

        started = time.monotonic()
        self.stdout.write(f"Seeding history {history_start()} .. {today()}")
        with transaction.atomic():  # all or nothing
            all_tenants, logins = tenants.seed()
            for label, module in STEPS:
                for t in all_tenants.values():
                    self.stdout.write(f"  {t.business.name:<16} {label}")
                    module.seed(t)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone in {time.monotonic() - started:.0f}s. Password for every login: {PASSWORD}\n"
        ))
        for username, role, business in logins:
            self.stdout.write(f"  {username:<34} {role:<28} {business}")
