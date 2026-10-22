"""QA sweep: routes, standing orders, runs and entries. A failing test is a finding."""
import threading
from datetime import timedelta

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.routes.models import Route, RouteRun
from tests.routes.test_routes import Base

FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
UNKNOWN = "11111111-1111-1111-1111-111111111111"


@FAST
class RouteSetupQA(Base, TestCase):
    def setUp(self):
        self.make()

    def route(self, user=None, **kw):
        body = {"name": "North", "assigned_to": str(self.m_boy.pk), **kw}
        return self.as_(user or self.manager).post(self.u("route-list-create"), body, format="json")

    def test_create_and_roles(self):
        self.assertEqual(self.route().status_code, 201)
        self.assertEqual(self.route(self.boy, name="X").status_code, 403)
        self.assertEqual(APIClient().post(self.u("route-list-create"), {"name": "x"}, format="json").status_code, 401)

    def test_duplicate_route_name_is_400_not_500(self):
        self.route()
        r = self.route()
        self.assertEqual(r.status_code, 400, r.content)

    def test_validation(self):
        for kw in ({"name": ""}, {"name": "  "}, {"name": "x" * 101}, {"name": None}, {"assigned_to": "nope"},
                   {"assigned_to": UNKNOWN}, {"bogus": 1}):
            r = self.route(**kw)
            self.assertEqual(r.status_code, 400, (kw, r.content))
        self.assertEqual(self.route(name="Hindi मार्ग").status_code, 201)

    def test_cannot_assign_a_member_of_another_business(self):
        from apps.businesses.models import BusinessMembership
        from apps.businesses.services import create_business
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.create_user("zz_out", password="x-pass-123")
        b2 = create_business(name="B2", slug="b2-dairy", owner=u)
        foreign = BusinessMembership.objects.get(business=b2, user=u)
        self.assertEqual(self.route(assigned_to=str(foreign.pk)).status_code, 400)

    def test_staff_sees_only_assigned_routes_and_list_is_ordered(self):
        self.route(name="B-route")
        self.route(name="A-route", assigned_to=str(self.m_boy2.pk))
        names = [r["name"] for r in self.as_(self.manager).get(self.u("route-list-create")).json()]
        self.assertEqual(names, sorted(names))
        names = [r["name"] for r in self.as_(self.boy).get(self.u("route-list-create")).json()]
        self.assertEqual(names, ["B-route"])

    def test_set_customers_rules(self):
        rid = self.route().json()["id"]
        url = self.u("route-customers", rid)
        ids = [str(c.pk) for c in self.cust]
        self.assertEqual(self.as_(self.manager).put(url, {"customers": ids}, format="json").status_code, 200)
        order = [r["customer"] for r in self.as_(self.manager).get(url).json()]
        self.assertEqual(order, ids)
        bad = ({"customers": ids + ids[:1]}, {"customers": [UNKNOWN]}, {"customers": "x"}, {}, {"customers": [1]},
               {"customers": ids, "bogus": 1}, {"customers": [str(self.cust[0].pk)] * 2},
               {"customers": [UNKNOWN] * 5001})
        for body in bad:
            r = self.as_(self.manager).put(url, body, format="json")
            self.assertEqual(r.status_code, 400, (str(body)[:60], r.content[:120]))
        self.assertEqual(self.as_(self.boy).put(url, {"customers": ids}, format="json").status_code, 403)

    def test_inactive_and_deleted_customers_cannot_join_a_route(self):
        rid = self.route().json()["id"]
        url = self.u("route-customers", rid)
        Customer.objects.filter(pk=self.cust[0].pk).update(is_active=False)
        self.assertEqual(self.as_(self.manager).put(url, {"customers": [str(self.cust[0].pk)]}, format="json").status_code, 400)
        self.cust[1].delete()
        self.assertEqual(self.as_(self.manager).put(url, {"customers": [str(self.cust[1].pk)]}, format="json").status_code, 400)

    def test_empty_list_clears_route(self):
        rid = self.setup_route()
        self.assertEqual(self.as_(self.manager).put(self.u("route-customers", rid), {"customers": []}, format="json").json(), [])

    def test_staff_cannot_read_other_peoples_route(self):
        rid = self.route(assigned_to=str(self.m_boy2.pk)).json()["id"]
        self.assertEqual(self.as_(self.boy).get(self.u("route-customers", rid)).status_code, 403)
        self.assertEqual(self.as_(self.boy).get(self.u("route-customers", UNKNOWN)).status_code, 404)


@FAST
class StandingOrderQA(Base, TestCase):
    def setUp(self):
        self.make()
        self.url = self.u("standing-order-list-create")

    def order(self, **kw):
        body = {"customer_id": str(self.cust[0].pk), "product_id": str(self.cow.pk), "quantity": "2", **kw}
        return self.as_(self.manager).post(self.url, body, format="json")

    def test_create_update_in_place_and_delete(self):
        a = self.order()
        self.assertEqual(a.status_code, 201, a.content)
        b = self.order(quantity="3")
        self.assertIn(b.status_code, (200, 201))
        self.assertEqual(self.as_(self.manager).get(self.url).json()["count"], 1)
        d = self.u("standing-order-detail", a.json()["id"])
        self.assertEqual(self.as_(self.manager).delete(d).status_code, 204)
        self.assertIn(self.as_(self.manager).delete(d).status_code, (204, 404))  # idempotent

    def test_validation(self):
        for kw in ({"quantity": "0"}, {"quantity": "-1"}, {"quantity": "abc"}, {"quantity": "1.2345"}, {"quantity": "NaN"},
                   {"shift": "NIGHT"}, {"customer_id": UNKNOWN}, {"product_id": UNKNOWN}, {"customer_id": "x"}, {"bogus": 1}):
            r = self.order(**kw)
            self.assertIn(r.status_code, (400, 404), (kw, r.content))
            self.assertNotEqual(r.status_code, 500)

    def test_inactive_customer_or_product_refused(self):
        Customer.objects.filter(pk=self.cust[0].pk).update(is_active=False)
        self.assertEqual(self.order().status_code, 400)

    def test_staff_forbidden_and_filter(self):
        self.assertEqual(self.as_(self.boy).get(self.url).status_code, 403)
        self.order()
        self.assertEqual(self.as_(self.manager).get(self.url, {"customer": str(self.cust[1].pk)}).json()["count"], 0)
        self.assertEqual(self.as_(self.manager).get(self.url, {"customer": "x"}).status_code, 400)


@FAST
class RunQA(Base, TestCase):
    def setUp(self):
        self.make()
        self.setup_route()

    def test_start_rules(self):
        self.assertEqual(self.as_(self.manager).post(self.u("run-list-start"), {"route_id": self.route_id}, format="json").status_code, 201)
        again = self.as_(self.manager).post(self.u("run-list-start"), {"route_id": self.route_id}, format="json")
        self.assertIn(again.status_code, (200, 201, 400, 409))  # returns the existing run
        self.assertEqual(RouteRun.objects.count(), 1)
        for body in ({}, {"route_id": UNKNOWN}, {"route_id": "x"}, {"route_id": self.route_id, "shift": "NIGHT"},
                     {"route_id": self.route_id, "run_date": "2026-13-45"}, {"route_id": self.route_id, "bogus": 1}):
            r = self.as_(self.manager).post(self.u("run-list-start"), body, format="json")
            self.assertIn(r.status_code, (400, 404), body)

    def test_future_run_date_is_refused(self):
        day = (timezone.now() + timedelta(days=10)).date().isoformat()
        self.assertEqual(self.as_(self.manager).post(self.u("run-list-start"), {"route_id": self.route_id, "run_date": day}, format="json").status_code, 400)

    def test_inactive_route_cannot_start(self):
        Route.objects.filter(pk=self.route_id).update(is_active=False)
        self.assertIn(self.as_(self.manager).post(self.u("run-list-start"), {"route_id": self.route_id}, format="json").status_code, (400, 404))

    def test_staff_only_sees_and_works_own_runs(self):
        self.start()
        self.assertEqual(self.as_(self.boy).get(self.u("run-list-start")).json()["count"], 1)
        self.assertEqual(self.as_(self.boy2).get(self.u("run-list-start")).json()["count"], 0)
        self.assertEqual(self.as_(self.boy2).get(self.u("run-tasks", self.run_id)).status_code, 404)
        self.assertEqual(self.entry(self.boy2, self.cust[0], items=self.basket()).status_code, 404)

    def test_filters_validation(self):
        self.start()
        for q in ({"date": "x"}, {"status": "NOPE"}):
            self.assertEqual(self.as_(self.manager).get(self.u("run-list-start"), q).status_code, 400, q)

    def test_money_and_quantities_are_strings_in_tasks(self):
        self.start()
        import json
        raw = self.as_(self.manager).get(self.u("run-tasks", self.run_id)).content.decode()
        self.assertNotRegex(raw, r'"(quantity|amount|price)[a-z_]*":\s*\d+\.\d+[,}]')

    def test_entry_rules(self):
        self.start()
        ok = self.entry(self.boy, self.cust[0], items=self.basket())
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(Delivery.objects.filter(status="DELIVERED").count(), 1)
        bad = [dict(outcome="DELIVERED"), dict(outcome="DELIVERED", items=[]), dict(outcome="BOGUS"),
               dict(items=[{"product_id": str(self.cow.pk), "quantity": "0"}]),
               dict(items=[{"product_id": str(self.cow.pk), "quantity": "-2"}]),
               dict(items=[{"product_id": str(self.cow.pk), "quantity": "1.2345"}]),
               dict(items=[{"product_id": UNKNOWN, "quantity": "1"}]),
               dict(items=[{"product_id": str(self.cow.pk), "quantity": "1", "bogus": 1}]),
               dict(items=self.basket(), bogus=1), dict(items=self.basket(), client_ref="x" * 41),
               dict(items=[{"product_id": str(self.cow.pk), "quantity": "1"}] * 500)]
        for kw in bad:
            r = self.entry(self.boy, self.cust[1], **kw)
            self.assertIn(r.status_code, (400, 404), (str(kw)[:80], r.content[:120]))

    def test_customer_not_on_route_and_foreign_customer(self):
        self.start()
        stray = Customer.objects.create(business=self.business, customer_code="STRAY", name="s")
        self.assertIn(self.entry(self.boy, stray, items=self.basket()).status_code, (400, 404))

    def test_unit_price_override_is_not_free_for_every_product(self):
        """A delivery boy must not be able to sell a priced product at a price he types."""
        self.start()
        r = self.entry(self.boy, self.cust[0], items=[{"product_id": str(self.cow.pk), "quantity": "1", "unit_price": "1.00"}])
        self.assertEqual(r.status_code, 400, r.content)

    def test_open_price_product_needs_a_price(self):
        self.start()
        r = self.entry(self.boy, self.cust[0], items=[{"product_id": str(self.other.pk), "quantity": "1"}])
        self.assertEqual(r.status_code, 400)
        r = self.entry(self.boy, self.cust[0], items=[{"product_id": str(self.other.pk), "quantity": "1", "unit_price": "25.00"}])
        self.assertEqual(r.status_code, 200, r.content)

    def test_skip_and_reason_and_replay(self):
        self.start()
        a = self.entry(self.boy, self.cust[0], items=self.basket(), client_ref="e1")
        b = self.entry(self.boy, self.cust[0], items=self.basket(), client_ref="e1")
        self.assertEqual((a.status_code, b.status_code), (200, 200))
        self.assertEqual(Delivery.objects.filter(status="DELIVERED").count(), 1)
        c = self.entry(self.boy, self.cust[0], items=self.basket("5"), client_ref="e1")
        self.assertIn(c.status_code, (400, 409))
        self.assertEqual(Delivery.objects.filter(status="DELIVERED").count(), 1)

    def test_close_needs_every_visit_recorded_then_locks_and_reopen_rules(self):
        self.start()
        self.assertEqual(self.as_(self.boy).post(self.u("run-close", self.run_id)).status_code, 400)
        for c in self.cust:
            self.entry(self.boy, c, items=self.basket())
        self.assertEqual(self.as_(self.boy).post(self.u("run-close", self.run_id)).status_code, 200)
        self.assertIn(self.as_(self.boy).post(self.u("run-close", self.run_id)).status_code, (200, 400))
        self.assertEqual(self.entry(self.boy, self.cust[0], items=self.basket()).status_code, 400)
        rev = self.u("run-reopen", self.run_id)
        self.assertEqual(self.as_(self.boy).post(rev, {"reason": "x"}, format="json").status_code, 403)
        for body in ({}, {"reason": ""}, {"reason": "  "}, {"reason": "x" * 501}, {"reason": "r", "bogus": 1}):
            self.assertEqual(self.as_(self.manager).post(rev, body, format="json").status_code, 400, body)
        self.assertEqual(self.as_(self.manager).post(rev, {"reason": "missed one"}, format="json").status_code, 200)
        self.assertEqual(self.as_(self.manager).post(rev, {"reason": "again"}, format="json").status_code, 400)


@FAST
class RunRaceQA(Base, TransactionTestCase):
    def setUp(self):
        self.make()
        self.setup_route()

    def test_parallel_start_creates_one_run(self):
        codes = []

        def go():
            try:
                c = APIClient()
                c.force_authenticate(self.manager)
                codes.append(c.post(self.u("run-list-start"), {"route_id": self.route_id}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertEqual(RouteRun.objects.count(), 1)

    def test_parallel_entry_same_customer_delivers_once(self):
        self.start()
        codes = []

        def go():
            try:
                c = APIClient()
                c.force_authenticate(self.boy)
                codes.append(c.post(self.u("run-entry", self.run_id), {"customer_id": str(self.cust[0].pk), "outcome": "DELIVERED",
                                    "items": self.basket()}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertEqual(Delivery.objects.filter(status="DELIVERED").count(), 1)


@FAST
class RouteUpdateQA(Base, TestCase):
    def setUp(self):
        self.make()
        self.rid = self.setup_route()
        self.url = self.u("route-detail", self.rid)

    def patch(self, body, user=None):
        return self.as_(user or self.manager).patch(self.url, body, format="json")

    def test_rename_reassign_and_unassign(self):
        r = self.patch({"name": "Sector 5 Morning", "assigned_to": str(self.m_boy2.pk)})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["name"], r.json()["assigned_to"]), ("Sector 5 Morning", str(self.m_boy2.pk)))
        self.assertIsNone(self.patch({"assigned_to": None}).json()["assigned_to"])

    def test_validation_and_roles(self):
        for body in ({"name": ""}, {"name": "x" * 101}, {"assigned_to": UNKNOWN}, {"is_active": "maybe"}, {"bogus": 1}):
            self.assertEqual(self.patch(body).status_code, 400, str(body))
        self.assertEqual(self.patch({"name": "x"}, self.boy).status_code, 403)
        self.assertEqual(self.as_(self.manager).patch(self.u("route-detail", UNKNOWN), {"name": "x"}, format="json").status_code, 404)

    def test_duplicate_name_is_400_but_own_name_is_fine(self):
        self.as_(self.manager).post(self.u("route-list-create"), {"name": "South"}, format="json")
        self.assertEqual(self.patch({"name": "south"}).status_code, 400)
        self.assertEqual(self.patch({"name": "NORTH"}).status_code, 200)  # same route, new case

    def test_deactivate_needs_closed_runs_and_hides_route(self):
        self.start()
        self.assertEqual(self.patch({"is_active": False}).status_code, 400)
        for c in self.cust:
            self.entry(self.boy, c, items=self.basket())
        self.as_(self.boy).post(self.u("run-close", self.run_id))
        self.assertEqual(self.patch({"is_active": False}).status_code, 200)
        listed = lambda q="": [r["id"] for r in self.as_(self.manager).get(self.u("route-list-create") + q).json()]
        self.assertNotIn(self.rid, listed())
        self.assertIn(self.rid, listed("?is_active=all"))
        self.assertEqual(self.as_(self.manager).get(self.u("route-list-create"), {"is_active": "x"}).status_code, 400)
        self.assertEqual(self.as_(self.manager).post(self.u("run-list-start"), {"route_id": self.rid}, format="json").status_code in (400, 404), True)
        self.assertEqual(self.patch({"is_active": True}).status_code, 200)
        self.assertIn(self.rid, listed())
