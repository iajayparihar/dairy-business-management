"""Accepted calendar range. A date like 9999-12-31 overflows `date + 1 day` and crashed endpoints."""
from datetime import date

MIN_YEAR, MAX_YEAR = 2000, 2100


def sane_date(value):
    """Return the date, or raise ValueError (every parser already turns that into a 400)."""
    if not MIN_YEAR <= value.year <= MAX_YEAR:
        raise ValueError(f"The year must be between {MIN_YEAR} and {MAX_YEAR}.")
    return value


def parse_iso_date(text):
    return sane_date(date.fromisoformat(text))
