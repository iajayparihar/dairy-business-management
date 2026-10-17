"""PostgreSQL text cannot hold NUL: any such input is a 400, never a 500."""
from tests.inventory.base import InvBase


class NullByteTests(InvBase):
    NUL = "a\u0000b"

    def base(self):
        return f"/api/v1/businesses/{self.business.pk}"

    def line(self):
        return [{"product_id": str(self.paneer.pk), "quantity": "1"}]

    def test_query_values_are_rejected(self):
        for path in ("customers/", "stock/", "sales/", "stock/adjustments/", "stock/production/"):
            r = self.api(self.owner).get(f"{self.base()}/{path}?search=a%00b")
            self.assertEqual(r.status_code, 400, path)
        self.assertEqual(self.api(self.owner).get(f"{self.base()}/customers/?search=ravi").status_code, 200)

    def test_json_strings_are_rejected_wherever_they_nest(self):
        c = self.api(self.owner)
        bad = {
            "sales/": {"client_ref": "n1", "payment_mode": "CASH", "notes": self.NUL, "lines": self.line()},
            "sales/ ": {"client_ref": "n2", "payment_mode": "CASH",
                        "lines": [{"description": self.NUL, "amount": "5"}]},
            "stock/adjustments/": {"client_ref": self.NUL, "kind": "WASTAGE",
                                   "product_id": str(self.paneer.pk), "quantity": "1", "reason": "abc"},
            "stock/production/": {"client_ref": "n3", "notes": self.NUL,
                                  "inputs": [{"product_id": str(self.cow.pk), "quantity": "1"}],
                                  "outputs": line_of(self.paneer)},
            "sales/quick-customer/": {"name": self.NUL, "phone": "123"},
        }
        for path, body in bad.items():
            r = c.post(f"{self.base()}/{path.strip()}", body, format="json")
            self.assertEqual(r.status_code, 400, path)
            self.assertEqual(r.json()["detail"], "Null characters are not allowed.")

    def test_clean_requests_still_work(self):
        r = self.api(self.owner).post(f"{self.base()}/sales/", {
            "client_ref": "ok1", "payment_mode": "CASH", "notes": "back\\u0000slash text",
            "lines": self.line()}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_malformed_json_is_still_the_views_400(self):
        r = self.api(self.owner).generic(
            "POST", f"{self.base()}/sales/", b"{not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)


def line_of(product):
    return [{"product_id": str(product.pk), "quantity": "1"}]
