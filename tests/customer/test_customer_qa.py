"""QA sweep: customers. A failing test is a finding."""
import threading

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import create_business
from apps.customers.models import Customer

User = get_user_model()


FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


class Base:
    def setUp(self):
        self.owner = User.objects.create_user("cq_owner", "o@x.com", "pw-123456!x")
        self.manager = User.objects.create_user("cq_mgr", "m@x.com", "pw-123456!x")
        self.staff = User.objects.create_user("cq_staff", "s@x.com", "pw-123456!x")
        self.outsider = User.objects.create_user("cq_out", "x@x.com", "pw-123456!x")
        self.biz = create_business(name="CQ Dairy", slug="cq-dairy", owner=self.owner)
        self.other = create_business(name="CQ Other", slug="cq-other", owner=self.outsider)
        for u, r in ((self.manager, "MANAGER"), (self.staff, "STAFF")):
            BusinessMembership.objects.create(business=self.biz, user=u, role=r, is_active=True)
        self.url = f"/api/v1/businesses/{self.biz.pk}/customers/"

    def api(self, user=None):
        c = APIClient()
        if user:
            c.force_authenticate(user)
        return c

    def mk(self, code="C1", name="Ramesh", **kw):
        return Customer.objects.create(business=self.biz, customer_code=code, name=name, **kw)

    def post(self, data, user=None):
        return self.api(user or self.owner).post(self.url, data, format="json")


@FAST
class CustomerCreateQA(Base, TestCase):
    def test_minimal_create(self):
        r = self.post({"customer_code": "C1", "name": "Ramesh"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["customer_type"], "HOUSEHOLD")
        self.assertTrue(r.json()["is_active"])

    def test_manager_can_staff_cannot_anonymous_cannot(self):
        self.assertEqual(self.post({"customer_code": "M1", "name": "A"}, self.manager).status_code, 201)
        self.assertEqual(self.post({"customer_code": "S1", "name": "A"}, self.staff).status_code, 403)
        self.assertEqual(APIClient().post(self.url, {}, format="json").status_code, 401)
        self.assertEqual(self.post({"customer_code": "O1", "name": "A"}, self.outsider).status_code, 403)

    def test_duplicate_code_is_400_not_500(self):
        self.mk("DUP")
        r = self.post({"customer_code": "DUP", "name": "Other"})
        self.assertEqual(r.status_code, 400, r.content)

    def test_duplicate_code_case_and_space_variants(self):
        self.mk("ABC")
        for c in ("abc", " ABC ", "ABC\t"):
            self.assertEqual(self.post({"customer_code": c, "name": "x"}).status_code, 400, repr(c))

    def test_code_of_soft_deleted_customer_is_not_reusable_and_is_400(self):
        c = self.mk("GONE")
        c.delete()
        self.assertEqual(self.post({"customer_code": "GONE", "name": "New"}).status_code, 400)

    def test_same_code_in_two_businesses_is_fine(self):
        Customer.objects.create(business=self.other, customer_code="SAME", name="x")
        self.assertEqual(self.post({"customer_code": "SAME", "name": "y"}).status_code, 201)

    def test_required_and_blank(self):
        for body in ({}, {"name": "A"}, {"customer_code": "A"}, {"customer_code": "", "name": "A"},
                     {"customer_code": "A", "name": ""}, {"customer_code": "A", "name": "   "},
                     {"customer_code": None, "name": None}):
            self.assertEqual(self.post(body).status_code, 400, body)

    def test_code_with_spaces_or_symbols_is_rejected(self):
        # codes become part of bill / payment numbers: <CODE>-<YYMMDD>-<seq>
        for bad in ("A B", "A/B", "A\\B", "A#1", "ए1", "A" * 31, "A\nB"):
            self.assertEqual(self.post({"customer_code": bad, "name": "x"}).status_code, 400, repr(bad))

    def test_field_length_limits_are_400(self):
        self.assertEqual(self.post({"customer_code": "L1", "name": "n" * 201}).status_code, 400)
        self.assertEqual(self.post({"customer_code": "L2", "name": "n", "phone": "9" * 21}).status_code, 400)
        self.assertEqual(self.post({"customer_code": "L3", "name": "n", "email": "a" * 250 + "@x.com"}).status_code, 400)

    def test_invalid_choice_and_types_never_500(self):
        self.assertEqual(self.post({"customer_code": "T1", "name": "n", "customer_type": "VIP"}).status_code, 400)
        for v in (1, [], {}, True):
            self.assertLess(self.post({"customer_code": v, "name": v, "phone": v, "email": v}).status_code, 500)
        self.assertEqual(self.post({"customer_code": "N\x00", "name": "n"}).status_code, 400)

    def test_phone_must_look_like_a_phone(self):
        for bad in ("abc", "<script>", "12", "---"):
            self.assertEqual(self.post({"customer_code": "P1", "name": "n", "phone": bad}).status_code, 400, bad)
        for good in ("9876543210", "+91 98765 43210", "0712-2345678", ""):
            code = "G" + str(abs(hash(good)))[:6]
            self.assertEqual(self.post({"customer_code": code, "name": "n", "phone": good}).status_code, 201, good)

    def test_hindi_and_emoji_names_roundtrip(self):
        r = self.post({"customer_code": "H1", "name": "रमेश कुमार डेयरी", "address": "नीमच, मध्य प्रदेश"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["name"], "रमेश कुमार डेयरी")

    def test_name_is_trimmed(self):
        r = self.post({"customer_code": "TR", "name": "  Ramesh  "})
        self.assertEqual(r.json()["name"], "Ramesh")

    def test_client_cannot_set_server_fields(self):
        r = self.post({"customer_code": "SF", "name": "n", "id": "11111111-1111-1111-1111-111111111111",
                       "business": str(self.other.pk), "created_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 201)
        c = Customer.objects.get(customer_code="SF", business=self.biz)
        self.assertNotEqual(str(c.pk), "11111111-1111-1111-1111-111111111111")
        self.assertEqual(c.business_id, self.biz.pk)

    def test_unknown_fields_rejected(self):
        self.assertEqual(self.post({"customer_code": "UF", "name": "n", "balance": 5}).status_code, 400)

    def test_inactive_business_is_blocked(self):
        Business.objects.filter(pk=self.biz.pk).update(is_active=False)
        self.assertEqual(self.api(self.owner).get(self.url).status_code, 403)

    def test_malformed_business_id(self):
        self.assertEqual(self.api(self.owner).get("/api/v1/businesses/not-a-uuid/customers/").status_code, 404)

    def test_inactive_membership_blocked(self):
        BusinessMembership.objects.filter(user=self.manager).update(is_active=False)
        self.assertEqual(self.api(self.manager).get(self.url).status_code, 403)


@FAST
class CustomerListQA(Base, TestCase):
    def test_tenant_isolation_and_staff_can_read(self):
        self.mk("A1")
        Customer.objects.create(business=self.other, customer_code="B1", name="Theirs")
        r = self.api(self.staff).get(self.url)
        self.assertEqual([c["customer_code"] for c in r.json()["results"]], ["A1"])
        self.assertEqual(self.api(self.outsider).get(self.url).status_code, 403)

    def test_deleted_customers_hidden(self):
        self.mk("D1").delete()
        self.assertEqual(self.api(self.owner).get(self.url).json()["count"], 0)

    def test_search_name_phone_code_case_insensitive_and_hindi(self):
        self.mk("RAM01", "Ramesh Dairy", phone="9876543210")
        self.mk("SHY02", "श्याम", phone="9000000001")
        g = lambda q: [c["customer_code"] for c in self.api(self.owner).get(self.url, {"search": q}).json()]
        self.assertEqual(g("ramesh"), ["RAM01"])
        self.assertEqual(g("98765"), ["RAM01"])
        self.assertEqual(g("shy"), ["SHY02"])
        self.assertEqual(g("श्याम"), ["SHY02"])
        self.assertEqual(g("%"), [])
        self.assertEqual(g("_"), [])

    def test_search_matches_phone_typed_with_country_code_or_spaces(self):
        self.post({"customer_code": "PH1", "name": "A", "phone": "+91 98765 43210"})
        r = self.api(self.owner).get(self.url, {"search": "9876543210"})
        self.assertEqual(len(r.json()), 1)  # stored as +919876543210
        r = self.api(self.owner).get(self.url, {"search": "+91 98765 43210"})
        self.assertEqual(len(r.json()), 1)

    def test_search_does_not_leak_other_tenants(self):
        Customer.objects.create(business=self.other, customer_code="ZZZ", name="Secret")
        self.assertEqual(self.api(self.owner).get(self.url, {"search": "Secret"}).json(), [])

    def test_pagination_is_stable_with_duplicate_names(self):
        for i in range(7):
            self.mk(f"S{i}", "Same Name")
        seen = []
        for page in (1, 2, 3, 4):
            r = self.api(self.owner).get(self.url, {"page_size": 2, "page": page})
            seen += [c["id"] for c in r.json()["results"]]
        self.assertEqual(len(seen), 7)
        self.assertEqual(len(set(seen)), 7)

    def test_page_size_capped_and_bad_values_not_500(self):
        for q in ({"page_size": 100000}, {"page_size": "x"}, {"page": 0}, {"page": 999}, {"page": "x"},
                  {"page_size": -1}):
            self.assertLess(self.api(self.owner).get(self.url, q).status_code, 500, q)

    def test_filter_by_active_and_type(self):
        self.mk("ACT", "A")
        self.mk("OFF", "B", is_active=False)
        self.mk("SHP", "C", customer_type="SHOP")
        g = lambda q: sorted(c["customer_code"] for c in self.api(self.owner).get(self.url, q).json()["results"])
        self.assertEqual(g({"is_active": "true"}), ["ACT", "SHP"])
        self.assertEqual(g({"is_active": "false"}), ["OFF"])
        self.assertEqual(g({"customer_type": "SHOP"}), ["SHP"])
        self.assertEqual(self.api(self.owner).get(self.url, {"customer_type": "BAD"}).status_code, 400)

    def test_response_has_no_money_or_secrets(self):
        self.mk("NS")
        keys = set(self.api(self.owner).get(self.url).json()["results"][0])
        self.assertTrue(keys.isdisjoint({"balance", "outstanding", "password", "deleted_by"}))

    def test_query_count_is_constant(self):
        for i in range(30):
            self.mk(f"Q{i}", f"n{i}")
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            self.api(self.owner).get(self.url, {"page_size": 30})
        self.assertLess(len(ctx), 12)


@FAST
class CustomerDetailQA(Base, TestCase):
    def setUp(self):
        super().setUp()
        self.c = self.mk("D1", "Detail")
        self.durl = f"{self.url}{self.c.pk}/"

    def test_get_foreign_and_deleted_and_unknown_are_404(self):
        theirs = Customer.objects.create(business=self.other, customer_code="T", name="t")
        self.assertEqual(self.api(self.owner).get(f"{self.url}{theirs.pk}/").status_code, 404)
        self.assertEqual(self.api(self.owner).get(f"{self.url}11111111-1111-1111-1111-111111111111/").status_code, 404)
        self.c.delete()
        self.assertEqual(self.api(self.owner).get(self.durl).status_code, 404)

    def test_patch_fields(self):
        r = self.api(self.owner).patch(self.durl, {"name": "New", "phone": "9876543210", "is_active": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.c.refresh_from_db()
        self.assertEqual((self.c.name, self.c.is_active), ("New", False))

    def test_staff_cannot_patch_or_delete(self):
        self.assertEqual(self.api(self.staff).patch(self.durl, {"name": "x"}, format="json").status_code, 403)
        self.assertEqual(self.api(self.staff).delete(self.durl).status_code, 403)

    def test_put_requires_full_body(self):
        self.assertEqual(self.api(self.owner).put(self.durl, {"name": "x"}, format="json").status_code, 400)

    def test_patch_code_to_existing_code_is_400(self):
        self.mk("OTHER")
        self.assertEqual(self.api(self.owner).patch(self.durl, {"customer_code": "OTHER"}, format="json").status_code, 400)

    def test_code_is_frozen_once_customer_has_records(self):
        """Bill / payment numbers embed the code; renaming it could collide with a new customer's numbers."""
        from unittest import mock
        with mock.patch("apps.customers.serializers.customer_has_records", return_value=True):
            r = self.api(self.owner).patch(self.durl, {"customer_code": "NEWCODE"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_patch_unknown_and_server_fields_rejected_or_ignored_safely(self):
        r = self.api(self.owner).patch(self.durl, {"business": str(self.other.pk)}, format="json")
        self.assertIn(r.status_code, (200, 400))
        self.c.refresh_from_db()
        self.assertEqual(self.c.business_id, self.biz.pk)

    def test_delete_without_records_soft_deletes_and_is_idempotent(self):
        self.assertEqual(self.api(self.owner).delete(self.durl).status_code, 204)
        self.assertTrue(Customer.all_objects.get(pk=self.c.pk).is_deleted)
        self.assertEqual(self.api(self.owner).delete(self.durl).status_code, 404)

    def test_delete_records_who_and_when(self):
        self.api(self.owner).delete(self.durl)
        c = Customer.all_objects.get(pk=self.c.pk)
        self.assertIsNotNone(c.deleted_at)
        self.assertEqual(c.deleted_by_id, self.owner.pk)

    def test_delete_blocked_when_customer_has_history(self):
        from unittest import mock
        with mock.patch("apps.customers.views.customer_has_records", return_value=True):
            self.assertEqual(self.api(self.owner).delete(self.durl).status_code, 400)
        self.assertFalse(Customer.all_objects.get(pk=self.c.pk).is_deleted)

    def test_deactivating_keeps_history_visible(self):
        self.api(self.owner).patch(self.durl, {"is_active": False}, format="json")
        self.assertEqual(self.api(self.owner).get(self.durl).status_code, 200)

    def test_methods(self):
        self.assertEqual(self.api(self.owner).post(self.durl, {}, format="json").status_code, 405)
        self.assertEqual(self.api(self.owner).put(self.url, {}, format="json").status_code, 405)
        self.assertEqual(self.api(self.owner).delete(self.url).status_code, 405)


@FAST
class CustomerRaceQA(Base, TransactionTestCase):
    def test_parallel_create_same_code_one_wins_no_500(self):
        codes = []

        def go():
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                codes.append(c.post(self.url, {"customer_code": "RACE", "name": "n"}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertEqual(codes.count(201), 1, codes)
