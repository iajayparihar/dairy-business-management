"""
Shared helpers for the demo seed (`python manage.py seed_demo`).

Every money / stock row is written through the service layer, so ledgers,
stock, bills and balances are exactly what the app itself would produce.
Only setup rows that services refuse on purpose (backdated selling prices)
are inserted directly.
"""
import random
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

TZ = ZoneInfo("Asia/Kolkata")
PASSWORD = "Dairy@12345"

# Fixed seed: same shape of data on every run (dates move with today).
rng = random.Random(2026)


def today():
    return timezone.now().astimezone(TZ).date()


def history_start():
    """First day of the previous month: one full month to bill + this month so far."""
    first = today().replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


def this_month():
    return today().replace(day=1)


def history_days():
    day = history_start()
    while day <= today():
        yield day
        day += timedelta(days=1)


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


def is_past(moment):
    return moment <= timezone.now()


def recent(day):
    """Inside the 48-hour staff window (yesterday or today)."""
    return day >= today() - timedelta(days=1)


def dec(value, places="0.001"):
    return Decimal(str(value)).quantize(Decimal(places))


def money(value):
    return dec(value, "0.01")


def vary(base, spread=0.12, places="0.1"):
    """base +/- spread, rounded: daily litres never look copy-pasted."""
    return dec(float(base) * rng.uniform(1 - spread, 1 + spread), places)


def worker(t, day):
    """Who records a day's work: staff for the last 48 hours, else the manager."""
    return t.staff[0] if recent(day) and t.staff else t.manager
