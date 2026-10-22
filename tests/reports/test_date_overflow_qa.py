"""Extreme dates must be a 400 (or an empty result), never a 500, on every dated endpoint."""
from django.test import override_settings

from tests.reports.test_tables import TableBase, ist

FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
EXTREME = ["9999-12-31", "0001-01-01", "2099-12-31", "1999-12-31", "10000-01-01", "2026-02-30"]


@FAST
class DateOverflowQA(TableBase):
    def test_extreme_dates_never_500_anywhere(self):
        b = f"/api/v1/businesses/{self.business.pk}/"
        sup, cust = self.s1.pk, self.customer.pk
        gets = []
        for d in EXTREME:
            gets += [
                (f"{b}suppliers/{sup}/statement/", {"start_date": d, "end_date": d}),
                (f"{b}suppliers/{sup}/statement/", {"end_date": d}),
                (f"{b}suppliers/{sup}/statement/print/", {"start_date": d, "end_date": d}),
                (f"{b}suppliers/{sup}/pay-summary/", {"start_date": d, "end_date": d}),
                (f"{b}customers/{cust}/statement/", {"start_date": d, "end_date": d}),
                (f"{b}sales/", {"start_date": d, "end_date": d}),
                (f"{b}sales/summary/", {"date": d}),
                (f"{b}milk-collections/", {"start_date": d, "end_date": d}),
                (f"{b}milk-collections/daily-summary/", {"date": d}),
                (f"{b}deliveries/", {"date": d}),
                (f"{b}deliveries/", {"date_from": d, "date_to": d}),
                (f"{b}expenses/", {"from": d, "to": d}),
                (f"{b}expenses/summary/", {"from": d, "to": d}),
                (f"{b}bills/", {"start_date": d, "end_date": d}),
                (f"{b}purchases/", {"date_from": d, "date_to": d}),
                (f"{b}route-runs/", {"date": d}),
                (f"{b}stock/{self.cow.pk}/", {"from": d, "to": d}),
            ]
        failures = []

        def hit(label, call):
            try:
                if call().status_code >= 500:
                    failures.append(label)
            except Exception as exc:  # an unhandled crash is the same bug
                failures.append((label, type(exc).__name__))

        for url, params in gets:
            hit((url.split("/v1/")[1][37:], params), lambda: self.api(self.owner).get(url, params))
        for d in EXTREME:
            hit(("bills/generate", d), lambda: self.api(self.owner).post(f"{b}bills/generate/", {"period_start": d, "period_end": d}, format="json"))
            hit(("customer bill", d), lambda: self.api(self.owner).post(f"{b}customers/{cust}/bills/", {"period_start": d, "period_end": d}, format="json"))
            hit(("expense", d), lambda: self.api(self.owner).post(f"{b}expenses/", {"client_ref": f"x{d}", "category_id": str(self.power.pk), "amount": "1.00", "expense_date": d}, format="json"))
        self.assertEqual(failures, [])
