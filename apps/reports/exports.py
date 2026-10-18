"""
JSON / CSV rendering of the report tables (apps/reports/tables.py).

CSV: UTF-8 with BOM (Excel + Hindi names), header row, money 2 dp, quantities
3 dp, no thousands separator, dates in Business.timezone, totals row last.
Text cells that could start a spreadsheet formula get a leading apostrophe.
"""
import csv
import re
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.text import slugify

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def fmt(kind, value, tz):
    """One cell as a plain string (None stays None)."""
    if value is None or value == "":
        return None
    if kind == "money":
        return f"{Decimal(value):.2f}"
    if kind == "qty":
        return f"{Decimal(value):.3f}"
    if kind == "datetime":
        return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    if kind == "date":
        return value.isoformat() if isinstance(value, date) else str(value)
    return str(value)


def _deep(value, tz):
    if isinstance(value, dict):
        return {k: _deep(v, tz) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep(v, tz) for v in value]
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, datetime):
        return value.astimezone(tz).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def table_cells(table, tz):
    """[[str]] data rows in column order (None -> "")."""
    cols = table["columns"]
    return [[fmt(kind, r.get(key), tz) or "" for key, _, kind in cols] for r in table["rows"]]


def totals_cells(table, tz):
    if not table["totals"]:
        return None
    cols = table["columns"]
    cells = [fmt(kind, table["totals"].get(key), tz) or "" for key, _, kind in cols]
    cells[0] = cells[0] or "TOTAL"
    return cells


def render_json(table, business, params, rows=None, extra=None):
    tz = ZoneInfo(business.timezone)
    cols = table["columns"]
    shown = table["rows"] if rows is None else rows
    body = {
        "report": table["key"], "title": table["title"],
        "business": {"id": str(business.pk), "name": business.name,
                     "timezone": business.timezone, "currency": business.currency},
        "params": params, "basis": "live",
        "generated_at": timezone.now().astimezone(tz).isoformat(),
        "columns": [{"key": k, "label": l, "kind": kind} for k, l, kind in cols],
        "rows": [{key: fmt(kind, r.get(key), tz) for key, _, kind in cols} for r in shown],
        "totals": {key: fmt(kind, table["totals"].get(key), tz)
                   for key, _, kind in cols if key in table["totals"]},
        "meta": _deep(table["meta"], tz),
        "warnings": table["warnings"],
    }
    body.update(extra or {})
    return body


def safe_filename(table, business, params, ext):
    parts = [table["key"], slugify(business.name) or "business"]
    parts += [slugify(str(params[k])) for k in ("from", "to", "as_of", "date") if params.get(k)]
    return "_".join(p for p in parts if p)[:120] + "." + ext


class _Echo:
    def write(self, value):
        return value


def csv_response(table, business, params):
    tz = ZoneInfo(business.timezone)
    cols = table["columns"]
    text_cols = [i for i, (_, _, kind) in enumerate(cols) if kind == "text"]
    writer = csv.writer(_Echo())

    def safe(row):
        return [csv_safe(c) if i in text_cols else c for i, c in enumerate(row)]

    def lines():
        yield "﻿"
        yield writer.writerow([label for _, label, _ in cols])
        for row in table_cells(table, tz):
            yield writer.writerow(safe(row))
        totals = totals_cells(table, tz)
        if totals:
            yield writer.writerow(safe(totals))

    response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="{safe_filename(table, business, params, "csv")}"'
    )
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
