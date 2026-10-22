"""QA sweep: milk collection API. A failing test is a finding."""
import threading
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.purchases.models import MilkCollection
from apps.suppliers.models import Supplier, SupplierMilkRate

User = get_user_model()
FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
IST = "+05:30"


class Base:
    def setUp(self):
        mk = lambda n: User.objects.create_user(n, f"{n}@x.com", "pw-123456!x")
        self.owner, self.manager, self.staff, self.outsider = (mk(n) for n in ("mq_o", "mq_m", "mq_s", "mq_x"))
        self.biz = create_business(name="MQ", slug="mq-dairy", owner=self.owner)
        self.other = create_business(name="MQ2", slug="mq-other", owner=self.outsider)
        for u, r in ((self.manager, "MANAGER"), (self.staff, "STAFF")):
            BusinessMembership.objects.create(business=self.biz, user=u, role=r, is_active=True)
        self.sup = Supplier.objects.create(business=self.biz, supplier_code="RAM01", name="Ram")
        self.long_ago = timezone.now() - timedelta(days=400)
        for kind, rate in (("BUFFALO", "15.00"), ("COW", "40.00")):
            SupplierMilkRate.objects.create(business=self.biz, supplier=self.sup, milk_type=kind,
                                            rate=rate, effective_from=self.long_ago, created_by=self.owner)
        self.base = f"/api/v1/businesses/{self.biz.pk}/"
        self.url = f"{self.base}suppliers/{self.sup.pk}/milk-collections/"
        self.now = timezone.now().replace(microsecond=0)

    def api(self, user=None):
        c = APIClient()
        if user:
            c.force_authenticate(user)
        return c

    def body(self, **kw):
        d = {"milk_type": "BUFFALO", "collected_at": self.now.isoformat(), "litres": "10", "fat": "9"}
        d.update(kw)
        return {k: v for k, v in d.items() if v is not None}

    def post(self, user=None, **kw):
        return self.api(user or self.owner).post(self.url, self.body(**kw), format="json")


@FAST
class MilkCreateQA(Base, TestCase):
    def test_buffalo_and_cow_amounts_and_number_format(self):
        r = self.post()
        self.assertEqual(r.status_code, 201, r.content)
        j = r.json()
        self.assertEqual((j["amount"], j["applied_rate"], j["pricing_basis"]), ("1350.00", "15.00", "FAT"))
        self.assertRegex(j["collection_number"], r"^RAM01-\d{6}-[ME]-B\d+$")
        c = self.post(milk_type="COW", litres="12.5", fat=None).json()
        self.assertEqual(c["amount"], "500.00")  # fat is read back as 0.00 for cow, see QA_LOG
        self.assertRegex(c["collection_number"], r"-C\d+$")

    def test_roles(self):
        self.assertEqual(self.post(self.manager).status_code, 201)
        self.assertEqual(self.post(self.staff).status_code, 403)
        self.assertEqual(self.post(self.outsider).status_code, 403)
        self.assertEqual(APIClient().post(self.url, self.body(), format="json").status_code, 401)

    def test_cow_fat_key_rejected_even_zero_buffalo_needs_fat(self):
        self.assertEqual(self.post(milk_type="COW", fat="0").status_code, 400)
        self.assertEqual(self.post(milk_type="COW", fat="4").status_code, 400)
        self.assertEqual(self.post(fat=None).status_code, 400)
        self.assertEqual(self.post(fat="0").status_code, 400)

    def test_client_cannot_set_number_or_server_fields(self):
        self.assertEqual(self.post(collection_number="X-1").status_code, 400)
        for k in ("amount", "applied_rate", "status", "supplier", "id"):
            self.assertEqual(self.post(**{k: "1"}).status_code, 400, k)

    def test_numeric_validation(self):
        for kw in ({"litres": "0"}, {"litres": "-1"}, {"litres": "abc"}, {"litres": "NaN"}, {"litres": "Infinity"},
                   {"litres": "1.2345"}, {"litres": "9" * 13}, {"litres": None}, {"fat": "1.234"},
                   {"fat": "-1"}, {"fat": "abc"}, {"fat": "99999"}, {"litres": [1]}, {"litres": {"a": 1}}):
            r = self.post(**kw)
            self.assertEqual(r.status_code, 400, kw)

    def test_timestamp_validation(self):
        for v in ("2026-01-01T10:00:00", "2026-01-01", "garbage", "2026-13-45T10:00:00Z", 12345, "", None):
            self.assertEqual(self.post(collected_at=v).status_code, 400, v)

    def test_future_and_ancient_timestamps_are_refused(self):
        far = (timezone.now() + timedelta(days=30)).isoformat()
        self.assertEqual(self.post(collected_at=far).status_code, 400)
        self.assertEqual(self.post(collected_at=(timezone.now() + timedelta(minutes=2)).isoformat()).status_code, 201)
        self.assertEqual(self.post(collected_at="1990-01-01T10:00:00Z").status_code, 400)

    def test_no_rate_for_that_time_is_a_clear_400(self):
        SupplierMilkRate.objects.filter(milk_type="COW").delete()
        r = self.post(milk_type="COW", fat=None)
        self.assertEqual(r.status_code, 400)
        self.assertIn("rate", r.content.decode().lower())

    def test_shift_derivation_boundaries_in_business_timezone(self):
        # business timezone is Asia/Kolkata: 15:00 local is evening
        day = (timezone.now() - timedelta(days=2)).astimezone().date().isoformat()
        for stamp, shift in ((f"{day}T14:59:59{IST}", "MORNING"), (f"{day}T15:00:00{IST}", "EVENING"),
                             (f"{day}T09:29:59Z", "MORNING"), (f"{day}T09:30:00Z", "EVENING"),
                             (f"{day}T00:00:00{IST}", "MORNING")):
            r = self.post(collected_at=stamp)
            self.assertEqual(r.status_code, 201, (stamp, r.content))
            self.assertEqual(r.json()["shift"], shift, stamp)

    def test_explicit_shift_wins_and_bad_shift_400(self):
        self.assertEqual(self.post(shift="EVENING").json()["shift"], "EVENING")
        self.assertEqual(self.post(shift="NIGHT").status_code, 400)

    def test_inactive_foreign_deleted_supplier(self):
        Supplier.objects.filter(pk=self.sup.pk).update(is_active=False)
        self.assertIn(self.post().status_code, (400, 404))
        theirs = Supplier.objects.create(business=self.other, supplier_code="T", name="t")
        url = f"{self.base}suppliers/{theirs.pk}/milk-collections/"
        self.assertIn(self.api(self.owner).post(url, self.body(), format="json").status_code, (400, 404))

    def test_malformed_body_never_500(self):
        for raw in ("[]", "null", "5", "{", '"x"'):
            r = self.api(self.owner).post(self.url, raw, content_type="application/json")
            self.assertLess(r.status_code, 500, raw)


@FAST
class MilkIdempotencyQA(Base, TestCase):
    def test_same_ref_same_data_replays(self):
        a = self.post(client_ref="r1")
        b = self.post(client_ref="r1")
        self.assertEqual(a.status_code, 201)
        self.assertIn(b.status_code, (200, 201))
        self.assertEqual(a.json()["id"], b.json()["id"])
        self.assertEqual(MilkCollection.objects.count(), 1)

    def test_same_ref_other_data_is_error(self):
        self.post(client_ref="r2")
        self.assertEqual(self.post(client_ref="r2", litres="11").status_code, 409)
        self.assertEqual(self.post(client_ref="r2", shift="EVENING").status_code, 409)
        self.assertEqual(MilkCollection.objects.count(), 1)

    def test_same_ref_for_another_supplier_does_not_replay(self):
        other = Supplier.objects.create(business=self.biz, supplier_code="SHY01", name="S")
        SupplierMilkRate.objects.create(business=self.biz, supplier=other, milk_type="BUFFALO", rate="15",
                                        effective_from=self.long_ago, created_by=self.owner)
        self.post(client_ref="r3")
        r = self.api(self.owner).post(f"{self.base}suppliers/{other.pk}/milk-collections/",
                                      self.body(client_ref="r3"), format="json")
        self.assertIn(r.status_code, (201, 400, 409))  # refs are unique per business
        self.assertEqual(MilkCollection.objects.filter(supplier=self.sup).count(), 1)

    def test_ref_validation(self):
        self.assertEqual(self.post(client_ref="x" * 65).status_code, 400)
        self.assertEqual(self.post(client_ref="").status_code, 400)
        self.assertEqual(self.post(client_ref="मिल्क-१").status_code, 201)

    def test_without_ref_two_identical_posts_make_two_cans(self):
        self.post()
        self.post()
        self.assertEqual(MilkCollection.objects.count(), 2)
        nums = list(MilkCollection.objects.values_list("collection_number", flat=True))
        self.assertEqual(len(set(nums)), 2)


@FAST
class MilkCancelCorrectQA(Base, TestCase):
    def setUp(self):
        super().setUp()
        self.c = self.post().json()
        self.d = f"{self.url}{self.c['id']}/"

    def test_cancel_rules(self):
        self.assertEqual(self.api(self.staff).post(self.d + "cancel/", {"reason": "x"}, format="json").status_code, 403)
        for body in ({}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}, {"reason": "ok", "x": 1}):
            self.assertEqual(self.api(self.owner).post(self.d + "cancel/", body, format="json").status_code, 400, body)
        r = self.api(self.owner).post(self.d + "cancel/", {"reason": "wrong can"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "CANCELLED")
        self.assertEqual(self.api(self.owner).post(self.d + "cancel/", {"reason": "again"}, format="json").status_code, 400)

    def test_cancelled_number_is_never_reused(self):
        self.api(self.owner).post(self.d + "cancel/", {"reason": "r"}, format="json")
        again = self.post().json()
        self.assertNotEqual(again["collection_number"], self.c["collection_number"])

    def test_correct_keeps_time_and_shift_and_links_replacement(self):
        r = self.api(self.owner).post(self.d + "correct/", {"reason": "weighed again", "litres": "11", "fat": "8.5"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        new = r.json()["collection"]
        self.assertEqual((new["litres"], new["fat"], new["shift"], new["collected_at"]),
                         ("11.000", "8.50", self.c["shift"], self.c["collected_at"]))
        self.assertEqual(new["replaces"], self.c["id"])
        self.assertEqual(self.api(self.owner).get(self.d).json()["status"], "CANCELLED")  # old can

    def test_correct_validation_and_state(self):
        ok = {"reason": "r", "litres": "5"}
        for body in ({}, {"reason": "r"}, {"litres": "5"}, {**ok, "litres": "0"}, {**ok, "fat": "0"}, {**ok, "x": 1}):
            self.assertEqual(self.api(self.owner).post(self.d + "correct/", body, format="json").status_code, 400, body)
        self.assertEqual(self.api(self.staff).post(self.d + "correct/", ok, format="json").status_code, 403)
        self.api(self.owner).post(self.d + "cancel/", {"reason": "r"}, format="json")
        self.assertEqual(self.api(self.owner).post(self.d + "correct/", ok, format="json").status_code, 400)

    def test_correct_cow_with_fat_is_400(self):
        cow = self.post(milk_type="COW", fat=None).json()
        r = self.api(self.owner).post(f"{self.url}{cow['id']}/correct/", {"reason": "r", "litres": "3", "fat": "4"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_foreign_and_unknown_ids(self):
        theirs = Supplier.objects.create(business=self.other, supplier_code="T", name="t")
        u = f"{self.base}suppliers/{theirs.pk}/milk-collections/{self.c['id']}/"
        self.assertEqual(self.api(self.owner).get(u).status_code, 404)
        wrong_supplier = f"{self.base}suppliers/{Supplier.objects.create(business=self.biz, supplier_code='Z', name='z').pk}/milk-collections/{self.c['id']}/"
        self.assertEqual(self.api(self.owner).get(wrong_supplier).status_code, 404)
        self.assertEqual(self.api(self.owner).get(f"{self.url}11111111-1111-1111-1111-111111111111/").status_code, 404)

    def test_no_delete_or_put(self):
        self.assertEqual(self.api(self.owner).delete(self.d).status_code, 405)
        self.assertEqual(self.api(self.owner).put(self.d, {}, format="json").status_code, 405)
        self.assertEqual(self.api(self.owner).patch(self.d, {"litres": "1"}, format="json").status_code, 405)


@FAST
class MilkListQA(Base, TestCase):
    def test_list_filters_pagination_isolation_and_perf(self):
        for i in range(6):
            self.post(litres=str(i + 1))
        self.post(milk_type="COW", fat=None)
        r = self.api(self.owner).get(self.base + "milk-collections/", {"page_size": 3}).json()
        self.assertEqual((r["count"], len(r["results"])), (7, 3))
        for q in ({"milk_type": "COW"}, {"status": "RECORDED"}, {"shift": "MORNING"}):
            self.assertEqual(self.api(self.owner).get(self.base + "milk-collections/", q).status_code, 200, q)
        for q in ({"milk_type": "GOAT"}, {"date_from": "x"}, {"date_from": "2026-13-45"}, {"page": "x"}):
            self.assertLess(self.api(self.owner).get(self.base + "milk-collections/", q).status_code, 500, q)
        self.assertEqual(self.api(self.staff).get(self.base + "milk-collections/").status_code, 403)
        self.assertEqual(self.api(self.outsider).get(self.base + "milk-collections/").status_code, 403)
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            self.api(self.owner).get(self.base + "milk-collections/", {"page_size": 7})
        self.assertLess(len(ctx), 15)

    def test_daily_summary(self):
        self.post(litres="10", fat="9")
        self.post(milk_type="COW", litres="5", fat=None)
        day = timezone.now().astimezone().date().isoformat()
        r = self.api(self.owner).get(self.base + "milk-collections/daily-summary/", {"date": day})
        self.assertEqual(r.status_code, 200, r.content)
        for bad in ("x", "2026-13-45", "0000-00-00"):
            self.assertEqual(self.api(self.owner).get(self.base + "milk-collections/daily-summary/", {"date": bad}).status_code, 400, bad)


@FAST
class MilkConcurrencyQA(Base, TransactionTestCase):
    def test_parallel_cans_get_unique_numbers_no_500(self):
        out = []

        def go(i):
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                out.append(c.post(self.url, self.body(litres=str(i + 1)), format="json"))
            finally:
                connection.close()

        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, [r.status_code for r in out])
        nums = [r.json()["collection_number"] for r in out if r.status_code == 201]
        self.assertEqual(len(nums), len(set(nums)))
        self.assertEqual(len(nums), 8)

    def test_parallel_same_client_ref_makes_one_can(self):
        out = []

        def go():
            try:
                c = APIClient()
                c.force_authenticate(self.owner)
                out.append(c.post(self.url, self.body(client_ref="same"), format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, out, out)
        self.assertEqual(MilkCollection.objects.count(), 1)

    def test_parallel_cancel_vs_correct(self):
        c = self.post().json()
        d = f"{self.url}{c['id']}/"
        out = []

        def go(path, body):
            try:
                cl = APIClient()
                cl.force_authenticate(self.owner)
                out.append((path, cl.post(d + path, body, format="json").status_code))
            finally:
                connection.close()

        ts = [threading.Thread(target=go, args=("cancel/", {"reason": "r"})),
              threading.Thread(target=go, args=("correct/", {"reason": "r", "litres": "3"}))] * 2
        ts = [threading.Thread(target=go, args=a) for a in (("cancel/", {"reason": "r"}), ("correct/", {"reason": "r", "litres": "3"}),
                                                            ("cancel/", {"reason": "r"}), ("correct/", {"reason": "r", "litres": "4"}))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, [s for _, s in out], out)
        self.assertEqual(MilkCollection.objects.filter(status="RECORDED").count() <= 1, True)
