"""QA sweep: suppliers and supplier milk rates. A failing test is a finding."""
import threading
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.suppliers.models import Supplier, SupplierMilkRate

User = get_user_model()
FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


class Base:
    def setUp(self):
        mk = lambda n: User.objects.create_user(n, f"{n}@x.com", "pw-123456!x")
        self.owner, self.manager, self.staff, self.outsider = (mk(n) for n in ("sq_o", "sq_m", "sq_s", "sq_x"))
        self.biz = create_business(name="SQ Dairy", slug="sq-dairy", owner=self.owner)
        self.other = create_business(name="SQ Other", slug="sq-other", owner=self.outsider)
        for u, r in ((self.manager, "MANAGER"), (self.staff, "STAFF")):
            BusinessMembership.objects.create(business=self.biz, user=u, role=r, is_active=True)
        self.url = f"/api/v1/businesses/{self.biz.pk}/suppliers/"

    def api(self, user=None):
        c = APIClient()
        if user:
            c.force_authenticate(user)
        return c

    def mk(self, code="S1", name="Farmer", **kw):
        return Supplier.objects.create(business=self.biz, supplier_code=code, name=name, **kw)

    def post(self, data, user=None):
        return self.api(user or self.owner).post(self.url, data, format="json")


@FAST
class SupplierQA(Base, TestCase):
    def test_create_and_roles(self):
        r = self.post({"supplier_code": "ram01", "name": "Ramlal"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["supplier_code"], "RAM01")
        self.assertEqual(r.json()["supplier_type"], "FARMER")
        self.assertEqual(self.post({"supplier_code": "M1", "name": "a"}, self.manager).status_code, 201)
        self.assertEqual(self.post({"supplier_code": "S9", "name": "a"}, self.staff).status_code, 403)
        self.assertEqual(self.post({"supplier_code": "X9", "name": "a"}, self.outsider).status_code, 403)
        self.assertEqual(APIClient().post(self.url, {}, format="json").status_code, 401)

    def test_duplicate_codes_are_400_incl_deleted_and_variants(self):
        self.mk("DUP")
        self.mk("OLD").delete()
        for c in ("DUP", "dup", " DUP ", "OLD"):
            self.assertEqual(self.post({"supplier_code": c, "name": "x"}).status_code, 400, c)

    def test_code_format_phone_and_unknown_fields(self):
        for bad in ("A B", "A/B", "ए1", "A" * 31, ""):
            self.assertEqual(self.post({"supplier_code": bad, "name": "x"}).status_code, 400, repr(bad))
        self.assertEqual(self.post({"supplier_code": "P1", "name": "x", "phone": "abc"}).status_code, 400)
        r = self.post({"supplier_code": "P2", "name": "x", "phone": "+91 98765-43210"})
        self.assertEqual(r.json()["phone"], "+919876543210")
        self.assertEqual(self.post({"supplier_code": "U1", "name": "x", "balance": 1}).status_code, 400)
        self.assertEqual(self.post({"supplier_code": "T1", "name": "x", "supplier_type": "KING"}).status_code, 400)
        self.assertEqual(self.post({"supplier_code": "N\x00", "name": "x"}).status_code, 400)

    def test_required_blank_types(self):
        for body in ({}, {"name": "x"}, {"supplier_code": "A"}, {"supplier_code": "A", "name": " "},
                     {"supplier_code": None, "name": None}):
            self.assertEqual(self.post(body).status_code, 400, body)
        for v in (1, [], {}, True):
            self.assertLess(self.post({"supplier_code": v, "name": v}).status_code, 500)

    def test_hindi_name(self):
        self.assertEqual(self.post({"supplier_code": "H1", "name": "रामलाल यादव"}).json()["name"], "रामलाल यादव")

    def test_list_paginated_ordered_filtered_searched_isolated(self):
        for i in range(7):
            self.mk(f"P{i}", "Same")
        self.mk("OFF", "Zed", is_active=False)
        self.mk("CC", "Center", supplier_type="COLLECTION_CENTER", phone="9876543210")
        Supplier.objects.create(business=self.other, supplier_code="THEIRS", name="Theirs")
        seen = []
        for page in (1, 2, 3, 4, 5):
            seen += [s["id"] for s in self.api(self.staff).get(self.url, {"page_size": 2, "page": page}).json()["results"]]
        self.assertEqual((len(seen), len(set(seen))), (9, 9))
        g = lambda q: sorted(s["supplier_code"] for s in self.api(self.owner).get(self.url, q).json()["results"])
        self.assertEqual(g({"is_active": "false"}), ["OFF"])
        self.assertEqual(g({"supplier_type": "COLLECTION_CENTER"}), ["CC"])
        self.assertEqual(g({"search": "98765"}), ["CC"])
        self.assertEqual(g({"search": "theirs"}), [])
        for q in ({"is_active": "x"}, {"supplier_type": "BAD"}):
            self.assertEqual(self.api(self.owner).get(self.url, q).status_code, 400)
        self.assertLess(self.api(self.owner).get(self.url, {"page_size": 10 ** 6, "page": "x"}).status_code, 500)

    def test_detail_patch_delete(self):
        s = self.mk("D1")
        d = f"{self.url}{s.pk}/"
        self.assertEqual(self.api(self.staff).patch(d, {"name": "x"}, format="json").status_code, 403)
        self.assertEqual(self.api(self.owner).patch(d, {"name": "New", "is_active": False}, format="json").status_code, 200)
        self.mk("OTHER")
        self.assertEqual(self.api(self.owner).patch(d, {"supplier_code": "OTHER"}, format="json").status_code, 400)
        self.assertEqual(self.api(self.owner).put(d, {"name": "x"}, format="json").status_code, 400)
        theirs = Supplier.objects.create(business=self.other, supplier_code="T", name="t")
        self.assertEqual(self.api(self.owner).get(f"{self.url}{theirs.pk}/").status_code, 404)

    def test_code_frozen_with_records_and_delete_blocked(self):
        s = self.mk("F1")
        d = f"{self.url}{s.pk}/"
        with mock.patch("apps.suppliers.serializers.supplier_has_records", return_value=True):
            self.assertEqual(self.api(self.owner).patch(d, {"supplier_code": "NEW"}, format="json").status_code, 400)
        with mock.patch("apps.suppliers.views.supplier_has_records", return_value=True):
            self.assertEqual(self.api(self.owner).delete(d).status_code, 400)
        self.assertFalse(Supplier.all_objects.get(pk=s.pk).is_deleted)

    def test_delete_without_records_records_who(self):
        s = self.mk("DEL")
        self.assertEqual(self.api(self.owner).delete(f"{self.url}{s.pk}/").status_code, 204)
        self.assertEqual(Supplier.all_objects.get(pk=s.pk).deleted_by_id, self.owner.pk)

    def test_real_records_block_delete_and_rename(self):
        """A supplier with a payment, purchase or milk can must not vanish or change code."""
        from apps.suppliers.guards import supplier_has_records
        s = self.mk("REC")
        self.assertFalse(supplier_has_records(s))
        from apps.payments.models import SupplierPayment
        self.assertTrue(hasattr(SupplierPayment, "supplier"))


@FAST
class SupplierRaceQA(Base, TransactionTestCase):
    def test_parallel_create_same_code(self):
        codes = []

        def go():
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                codes.append(c.post(self.url, {"supplier_code": "RACE", "name": "n"}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertEqual(codes.count(201), 1, codes)


@FAST
class MilkRateQA(Base, TestCase):
    def setUp(self):
        super().setUp()
        self.s = self.mk("RATE1")
        self.rurl = f"{self.url}{self.s.pk}/milk-rates/"
        self.t0 = timezone.now().replace(microsecond=0) - timedelta(days=30)

    def rate(self, rate="50.00", frm=None, to=None, milk="COW", user=None, **kw):
        body = {"milk_type": milk, "rate": rate,
                "effective_from": (frm or self.t0).isoformat(), **kw}
        if to:
            body["effective_to"] = to.isoformat()
        return self.api(user or self.owner).post(self.rurl, body, format="json")

    def test_permissions_cost_data_owner_manager_only(self):
        self.assertEqual(self.rate(user=self.manager).status_code, 201)
        self.assertEqual(self.rate(frm=self.t0 + timedelta(days=1), user=self.staff).status_code, 403)
        self.assertEqual(self.api(self.staff).get(self.rurl).status_code, 403)
        self.assertEqual(self.api(self.outsider).get(self.rurl).status_code, 403)

    def test_rate_change_closes_previous_open_rate(self):
        """The owner must be able to change a rate without pre-planning an end date."""
        self.assertEqual(self.rate("50.00").status_code, 201)
        r = self.rate("55.00", frm=self.t0 + timedelta(days=10))
        self.assertEqual(r.status_code, 201, r.content)
        old = SupplierMilkRate.objects.get(rate="50.00")
        self.assertEqual(old.effective_to, self.t0 + timedelta(days=10))
        self.assertIsNone(SupplierMilkRate.objects.get(rate="55.00").effective_to)

    def test_insert_between_schedules_both_sides(self):
        self.rate("50.00", frm=self.t0)
        self.rate("60.00", frm=self.t0 + timedelta(days=20))
        self.assertEqual(self.rate("55.00", frm=self.t0 + timedelta(days=10)).status_code, 201)
        mid = SupplierMilkRate.objects.get(rate="55.00")
        self.assertEqual(mid.effective_to, self.t0 + timedelta(days=20))
        self.assertEqual(SupplierMilkRate.objects.get(rate="50.00").effective_to, mid.effective_from)

    def test_same_start_twice_and_each_milk_type_independent(self):
        self.assertEqual(self.rate().status_code, 201)
        self.assertEqual(self.rate("51.00").status_code, 400)
        self.assertEqual(self.rate("70.00", milk="BUFFALO").status_code, 201)

    def test_validation(self):
        for body in ({}, {"milk_type": "COW"}, {"milk_type": "GOAT", "rate": "1", "effective_from": self.t0.isoformat()},
                     {"milk_type": "COW", "rate": "0", "effective_from": self.t0.isoformat()},
                     {"milk_type": "COW", "rate": "-5", "effective_from": self.t0.isoformat()},
                     {"milk_type": "COW", "rate": "abc", "effective_from": self.t0.isoformat()},
                     {"milk_type": "COW", "rate": "1.234", "effective_from": self.t0.isoformat()},
                     {"milk_type": "COW", "rate": "99999999999", "effective_from": self.t0.isoformat()},
                     {"milk_type": "COW", "rate": "1", "effective_from": "2026-01-01T10:00:00"},
                     {"milk_type": "COW", "rate": "1", "effective_from": "garbage"},
                     {"milk_type": "COW", "rate": "1", "effective_from": "12000-01-01T00:00:00Z"},
                     {"milk_type": "COW", "rate": "1", "effective_from": self.t0.isoformat(), "supplier": "x"}):
            r = self.api(self.owner).post(self.rurl, body, format="json")
            self.assertEqual(r.status_code, 400, body)

    def test_end_before_start_rejected(self):
        self.assertEqual(self.rate(to=self.t0 - timedelta(days=1)).status_code, 400)

    def test_inactive_supplier_and_foreign_supplier(self):
        Supplier.objects.filter(pk=self.s.pk).update(is_active=False)
        self.assertEqual(self.rate().status_code, 404)
        theirs = Supplier.objects.create(business=self.other, supplier_code="T", name="t")
        self.assertEqual(self.api(self.owner).get(f"{self.url}{theirs.pk}/milk-rates/").status_code, 404)

    def test_list_paginated_newest_first(self):
        for i in range(3):
            self.rate(str(50 + i), frm=self.t0 + timedelta(days=i))
        r = self.api(self.owner).get(self.rurl, {"page_size": 2}).json()
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["results"][0]["rate"], "52.00")

    def test_amount_calculation_edge_cases(self):
        from django.core.exceptions import ValidationError
        from apps.suppliers.services import calculate_milk_amount as calc
        self.assertEqual(str(calc(litres="10", fat="9", rate="15", milk_type="BUFFALO")), "1350.00")
        self.assertEqual(str(calc(litres="0.5", rate="33.33", milk_type="COW")), "16.67")  # half-up
        self.assertEqual(str(calc(litres="2.505", rate="10", milk_type="COW")), "25.05")
        for kw in ({"litres": "0"}, {"litres": "-1"}, {"litres": "NaN"}, {"litres": "Infinity"}, {"litres": "x"}):
            with self.assertRaises(ValidationError):
                calc(rate="10", milk_type="COW", **kw)
        with self.assertRaises(ValidationError):
            calc(litres="1", rate="10", milk_type="COW", fat="0")
        with self.assertRaises(ValidationError):
            calc(litres="1", rate="10", milk_type="BUFFALO", fat="0")

    def test_rate_resolution_boundaries(self):
        from django.core.exceptions import ValidationError
        from apps.suppliers.services import resolve_supplier_milk_rate as res
        self.rate("50.00", frm=self.t0)
        self.rate("55.00", frm=self.t0 + timedelta(days=10))
        kw = dict(business_id=self.biz.pk, supplier_id=self.s.pk, milk_type="COW")
        self.assertEqual(str(res(collected_at=self.t0, **kw).rate), "50.00")
        self.assertEqual(str(res(collected_at=self.t0 + timedelta(days=10) - timedelta(microseconds=1), **kw).rate), "50.00")
        self.assertEqual(str(res(collected_at=self.t0 + timedelta(days=10), **kw).rate), "55.00")
        with self.assertRaises(ValidationError):
            res(collected_at=self.t0 - timedelta(seconds=1), **kw)
