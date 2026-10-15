from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.deliveries.services import cancel_delivery, correct_delivery
from apps.inventory.models import StockMovement
from apps.products.models import Product, ProductPrice
from apps.routes.models import RouteRun, RouteVisit

User = get_user_model()


class Base:
    def make(self):
        mk = lambda n: User.objects.create_user(username=n, password="x-pass-123")
        self.owner, self.manager, self.boy, self.boy2 = (
            mk("r_owner"), mk("r_mgr"), mk("r_boy"), mk("r_boy2"),
        )
        self.business = create_business(name="R", slug="r-dairy", owner=self.owner)
        mem = lambda u, role: BusinessMembership.objects.create(business=self.business, user=u, role=role)
        mem(self.manager, "MANAGER")
        self.m_boy, self.m_boy2 = mem(self.boy, "STAFF"), mem(self.boy2, "STAFF")

        self.cust = [
            Customer.objects.create(business=self.business, customer_code=f"C{i}", name=f"Cust{i}")
            for i in range(3)
        ]
        self.cow = Product.objects.create(business=self.business, sku="COW", name="Cow", track_stock=True)
        self.dahi = Product.objects.create(business=self.business, sku="DAHI", name="Dahi", unit="KG")
        self.other = Product.objects.create(
            business=self.business, sku="OTH", name="Other", unit="PCS", allow_open_price=True,
        )
        for p, price in ((self.cow, "60"), (self.dahi, "100")):
            ProductPrice.objects.create(
                business=self.business, product=p, price_type="SELLING",
                unit_price=Decimal(price), effective_from=timezone.now() - timedelta(days=90),
            )
        self.c = APIClient()

    def as_(self, user):
        self.c.force_authenticate(user)
        return self.c

    def u(self, name, pk=None, business=None):
        kw = {"business_id": (business or self.business).pk}
        if pk is not None:
            kw["pk"] = pk
        return reverse(f"routes:{name}", kwargs=kw)

    def setup_route(self, assigned=None):
        r = self.as_(self.manager).post(
            self.u("route-list-create"),
            {"name": "North", "assigned_to": str((assigned or self.m_boy).pk)}, format="json",
        )
        assert r.status_code == 201, r.data
        self.route_id = r.data["id"]
        r = self.c.put(
            self.u("route-customers", self.route_id),
            {"customers": [str(c.pk) for c in self.cust]}, format="json",
        )
        assert r.status_code == 200, r.data
        return self.route_id

    def start(self, **kw):
        body = {"route_id": self.route_id, **kw}
        r = self.as_(self.manager).post(self.u("run-list-start"), body, format="json")
        assert r.status_code == 201, r.data
        self.run_id = r.data["id"]
        return r.data

    def entry(self, user, customer, outcome="DELIVERED", **kw):
        body = {"customer_id": str(customer.pk), "outcome": outcome, **kw}
        return self.as_(user).post(self.u("run-entry", self.run_id), body, format="json")

    def basket(self, qty="2"):
        return [{"product_id": str(self.cow.pk), "quantity": qty}]


class RouteSetupTests(Base, TestCase):
    def setUp(self):
        self.make()

    def test_routes_ordered_and_staff_forbidden_to_manage(self):
        self.setup_route()
        r = self.as_(self.boy).get(self.u("route-customers", self.route_id))
        self.assertEqual([x["name"] for x in r.data], ["Cust0", "Cust1", "Cust2"])
        r = self.c.put(self.u("route-customers", self.route_id), {"customers": []}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.c.post(self.u("route-list-create"), {"name": "X"}, format="json").status_code, 403)

    def test_reorder_and_customer_cannot_be_on_two_routes(self):
        self.setup_route()
        ids = [str(c.pk) for c in reversed(self.cust)]
        r = self.as_(self.manager).put(self.u("route-customers", self.route_id), {"customers": ids}, format="json")
        self.assertEqual([x["name"] for x in r.data], ["Cust2", "Cust1", "Cust0"])
        r2 = self.c.post(self.u("route-list-create"), {"name": "South"}, format="json").data["id"]
        r = self.c.put(self.u("route-customers", r2), {"customers": [str(self.cust[0].pk)]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_foreign_and_duplicate_customers_rejected(self):
        self.setup_route()
        other_owner = User.objects.create_user(username="r_oo", password="x-pass-123")
        other = create_business(name="O", slug="o-dairy", owner=other_owner)
        foreign = Customer.objects.create(business=other, customer_code="F", name="F")
        c = self.as_(self.manager)
        url = self.u("route-customers", self.route_id)
        self.assertEqual(c.put(url, {"customers": [str(foreign.pk)]}, format="json").status_code, 400)
        dup = str(self.cust[0].pk)
        self.assertEqual(c.put(url, {"customers": [dup, dup]}, format="json").status_code, 400)

    def test_assignee_must_belong_to_business(self):
        other_owner = User.objects.create_user(username="r_oo2", password="x-pass-123")
        other = create_business(name="O2", slug="o2-dairy", owner=other_owner)
        foreign = BusinessMembership.objects.get(business=other)
        r = self.as_(self.manager).post(self.u("route-list-create"), {"name": "Z", "assigned_to": str(foreign.pk)}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_staff_sees_only_own_routes(self):
        self.setup_route()
        self.assertEqual(len(self.as_(self.boy).get(self.u("route-list-create")).data), 1)
        self.assertEqual(len(self.as_(self.boy2).get(self.u("route-list-create")).data), 0)
        self.assertEqual(self.c.get(self.u("route-customers", self.route_id)).status_code, 403)

    def test_standing_order_upsert_and_remove(self):
        c = self.as_(self.manager)
        body = {"customer_id": str(self.cust[0].pk), "product_id": str(self.cow.pk), "quantity": "2"}
        a = c.post(self.u("standing-order-list-create"), body, format="json")
        b = c.post(self.u("standing-order-list-create"), {**body, "quantity": "3"}, format="json")
        self.assertEqual((a.status_code, a.data["id"]), (201, b.data["id"]))
        self.assertEqual(c.get(self.u("standing-order-list-create")).data["count"], 1)
        self.assertEqual(c.delete(self.u("standing-order-detail", a.data["id"])).status_code, 204)
        self.assertEqual(c.get(self.u("standing-order-list-create")).data["count"], 0)

    def test_standing_order_rules(self):
        c = self.as_(self.manager)
        pcs = {"customer_id": str(self.cust[0].pk), "product_id": str(self.other.pk), "quantity": "1.5"}
        self.assertEqual(c.post(self.u("standing-order-list-create"), pcs, format="json").status_code, 400)
        self.assertEqual(self.as_(self.boy).post(self.u("standing-order-list-create"), pcs, format="json").status_code, 403)


class RunFlowTests(Base, TestCase):
    def setUp(self):
        self.make()
        self.setup_route()

    def test_start_creates_ordered_pending_visits_and_is_idempotent(self):
        run = self.start()
        again = self.as_(self.manager).post(self.u("run-list-start"), {"route_id": self.route_id}, format="json")
        self.assertEqual(again.data["id"], run["id"])
        self.assertEqual(RouteVisit.objects.filter(run_id=run["id"]).count(), 3)
        tasks = self.as_(self.boy).get(self.u("run-tasks", run["id"])).data["tasks"]
        self.assertEqual([t["customer_name"] for t in tasks], ["Cust0", "Cust1", "Cust2"])
        self.assertTrue(all(t["status"] == "PENDING" for t in tasks))

    def test_future_run_and_staff_start_rejected(self):
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        r = self.as_(self.manager).post(self.u("run-list-start"), {"route_id": self.route_id, "run_date": tomorrow}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.as_(self.boy).post(self.u("run-list-start"), {"route_id": self.route_id}, format="json").status_code, 403)

    def test_standing_order_then_last_delivery_suggestions(self):
        self.as_(self.manager).post(self.u("standing-order-list-create"), {
            "customer_id": str(self.cust[0].pk), "product_id": str(self.cow.pk), "quantity": "2"}, format="json")
        # Cust1: no standing order, but a delivery yesterday.
        yday = timezone.now() - timedelta(days=1)
        Delivery.objects.create(
            business=self.business, customer=self.cust[1], product=self.dahi,
            delivered_at=yday, quantity=Decimal("0.5"), unit_price=Decimal("100"),
        )
        self.start()
        t = self.as_(self.boy).get(self.u("run-tasks", self.run_id)).data["tasks"]
        self.assertEqual((t[0]["suggestion_source"], t[0]["suggestion"][0]["product_name"]), ("standing_order", "Cow"))
        self.assertEqual((t[1]["suggestion_source"], Decimal(t[1]["suggestion"][0]["quantity"])), ("last_delivery", Decimal("0.5")))
        self.assertEqual((t[2]["suggestion_source"], t[2]["suggestion"]), (None, []))

    def test_suggestion_does_not_copy_a_price(self):
        yday = timezone.now() - timedelta(days=1)
        Delivery.objects.create(
            business=self.business, customer=self.cust[0], product=self.cow,
            delivered_at=yday, quantity=Decimal("2"), unit_price=Decimal("1"),
        )
        self.start()
        self.entry(self.boy, self.cust[0], items=self.basket("2"))
        d = Delivery.objects.get(visit__run_id=self.run_id)
        self.assertEqual(d.unit_price, Decimal("60.00"))

    def test_delivered_creates_deliveries_stock_and_marks_visit(self):
        self.start()
        r = self.entry(self.boy, self.cust[0], items=[
            *self.basket("2"), {"product_id": str(self.dahi.pk), "quantity": "0.5"},
            {"product_id": str(self.other.pk), "quantity": "1", "unit_price": "40"},
        ])
        self.assertEqual((r.status_code, r.data["status"]), (200, "DELIVERED"))
        ds = Delivery.objects.filter(visit__run_id=self.run_id)
        self.assertEqual(ds.count(), 3)
        self.assertEqual(sum(d.line_total for d in ds), Decimal("120") + Decimal("50") + Decimal("40"))
        self.assertEqual(StockMovement.objects.filter(delivery__in=ds).count(), 1)  # only cow tracks stock
        self.assertTrue(all(d.created_by == self.boy and d.shift == "MORNING" for d in ds))

    def test_absent_and_skipped_create_no_delivery(self):
        self.start()
        self.assertEqual(self.entry(self.boy, self.cust[0], "ABSENT").status_code, 200)
        r = self.entry(self.boy, self.cust[1], "SKIPPED")
        self.assertEqual(r.status_code, 400)  # reason required
        r = self.entry(self.boy, self.cust[1], "SKIPPED", reason="Gate locked")
        self.assertEqual(r.data["reason"], "Gate locked")
        self.assertFalse(Delivery.objects.exists())

    def test_non_delivery_with_items_rejected(self):
        self.start()
        self.assertEqual(self.entry(self.boy, self.cust[0], "ABSENT", items=self.basket()).status_code, 400)

    def test_delivered_without_items_rejected(self):
        self.start()
        self.assertEqual(self.entry(self.boy, self.cust[0], items=[]).status_code, 400)

    def test_failed_item_rolls_back_whole_entry(self):
        self.start()
        r = self.entry(self.boy, self.cust[0], items=[*self.basket("2"), {"product_id": str(self.dahi.pk), "quantity": "0.0001"}])
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Delivery.objects.exists())
        self.assertFalse(StockMovement.objects.filter(movement_type="DELIVERY").exists())
        self.assertEqual(RouteVisit.objects.get(run_id=self.run_id, customer=self.cust[0]).status, "PENDING")

    def test_retry_with_same_client_ref_is_safe(self):
        self.start()
        a = self.entry(self.boy, self.cust[0], items=self.basket(), client_ref="tap-1")
        b = self.entry(self.boy, self.cust[0], items=self.basket(), client_ref="tap-1")
        self.assertEqual((a.status_code, b.status_code), (200, 200))
        self.assertEqual(Delivery.objects.count(), 1)

    def test_same_basket_without_client_ref_is_rejected_as_duplicate(self):
        self.start()
        self.entry(self.boy, self.cust[0], items=self.basket())
        self.assertEqual(self.entry(self.boy, self.cust[0], items=self.basket()).status_code, 400)
        self.assertEqual(Delivery.objects.count(), 1)

    def test_extras_can_be_added_later_to_delivered_visit(self):
        self.start()
        self.entry(self.boy, self.cust[0], items=self.basket())
        r = self.entry(self.boy, self.cust[0], items=[{"product_id": str(self.dahi.pk), "quantity": "1"}])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Delivery.objects.filter(visit__run_id=self.run_id).count(), 2)

    def test_absent_can_become_delivered_but_delivered_cannot_become_absent(self):
        self.start()
        self.entry(self.boy, self.cust[0], "ABSENT")
        self.assertEqual(self.entry(self.boy, self.cust[0], items=self.basket()).data["status"], "DELIVERED")
        self.assertEqual(self.entry(self.boy, self.cust[0], "ABSENT").status_code, 400)

    def test_other_boy_cannot_work_or_see_run(self):
        self.start()
        self.assertEqual(self.as_(self.boy2).get(self.u("run-tasks", self.run_id)).status_code, 404)
        self.assertEqual(self.entry(self.boy2, self.cust[0], items=self.basket()).status_code, 404)
        self.assertEqual(self.as_(self.boy2).get(self.u("run-list-start")).data["count"], 0)
        self.assertEqual(self.as_(self.boy).get(self.u("run-list-start")).data["count"], 1)

    def test_manager_can_work_any_run(self):
        self.start()
        self.assertEqual(self.entry(self.manager, self.cust[0], items=self.basket()).status_code, 200)

    def test_customer_not_on_run_is_404(self):
        self.start()
        stranger = Customer.objects.create(business=self.business, customer_code="S", name="S")
        self.assertEqual(self.entry(self.boy, stranger, items=self.basket()).status_code, 404)

    def test_past_run_entry_is_timestamped_on_run_date(self):
        two_days_ago = timezone.localdate() - timedelta(days=2)
        self.start(run_date=two_days_ago.isoformat())
        self.entry(self.manager, self.cust[0], items=self.basket())
        d = Delivery.objects.get()
        self.assertEqual(d.delivery_date, two_days_ago)
        self.assertEqual(timezone.localtime(d.delivered_at).hour, 6)

    def test_evening_run_uses_evening_shift_and_is_separate(self):
        self.start()
        morning = self.run_id
        self.start(shift="EVENING")
        self.assertNotEqual(morning, self.run_id)
        self.entry(self.boy, self.cust[0], items=self.basket())
        self.assertEqual(Delivery.objects.get().shift, "EVENING")


class CloseReopenTests(Base, TestCase):
    def setUp(self):
        self.make()
        self.setup_route()
        self.start()

    def close(self, user=None):
        return self.as_(user or self.boy).post(self.u("run-close", self.run_id), {}, format="json")

    def finish_all(self):
        self.entry(self.boy, self.cust[0], items=self.basket("2"))
        self.entry(self.boy, self.cust[1], "ABSENT")
        self.entry(self.boy, self.cust[2], "NOT_DELIVERED", reason="Refused")

    def test_cannot_close_with_pending_visits(self):
        self.entry(self.boy, self.cust[0], items=self.basket())
        r = self.close()
        self.assertEqual(r.status_code, 400)
        self.assertIn("2 customer visit", str(r.data))
        self.assertEqual(RouteRun.objects.get(pk=self.run_id).status, "OPEN")

    def test_close_returns_totals(self):
        self.finish_all()
        r = self.close()
        self.assertEqual(r.status_code, 200)
        s = r.data["summary"]
        self.assertEqual((s["visits"]["DELIVERED"], s["visits"]["ABSENT"], s["visits"]["NOT_DELIVERED"]), (1, 1, 1))
        self.assertEqual(Decimal(s["total_amount"]), Decimal("120.00"))
        self.assertEqual(Decimal(s["products"][0]["quantity"]), Decimal("2.000"))
        self.assertEqual(s["stock_shortfalls"], 1)
        run = RouteRun.objects.get(pk=self.run_id)
        self.assertEqual((run.status, run.closed_by), ("CLOSED", self.boy))

    def test_closed_run_rejects_entries_and_double_close(self):
        self.finish_all(); self.close()
        self.assertEqual(self.entry(self.boy, self.cust[1], items=self.basket()).status_code, 400)
        self.assertEqual(self.close().status_code, 400)

    def test_other_boy_cannot_close(self):
        self.finish_all()
        self.assertEqual(self.close(self.boy2).status_code, 404)

    def test_closed_run_blocks_cancel_and_correct_until_reopened(self):
        self.finish_all(); self.close()
        d = Delivery.objects.get()
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            cancel_delivery(business_id=self.business.pk, delivery_id=d.pk, user=self.owner, reason="x")
        with self.assertRaises(ValidationError):
            correct_delivery(business_id=self.business.pk, delivery_id=d.pk, user=self.owner, reason="x", quantity="3")
        d.refresh_from_db()
        self.assertEqual(d.status, "DELIVERED")

        r = self.as_(self.manager).post(self.u("run-reopen", self.run_id), {"reason": "Wrong litres"}, format="json")
        self.assertEqual(r.status_code, 200)
        run = RouteRun.objects.get(pk=self.run_id)
        self.assertEqual((run.status, run.reopened_by, run.reopen_reason), ("OPEN", self.manager, "Wrong litres"))
        new = correct_delivery(business_id=self.business.pk, delivery_id=d.pk, user=self.owner, reason="x", quantity="3")
        self.assertEqual(new.visit_id, d.visit_id)
        self.assertEqual(self.close().status_code, 200)
        self.assertEqual(Decimal(self.c.get(self.u("run-tasks", self.run_id)).data["summary"]["total_amount"]), Decimal("180.00"))

    def test_reopen_needs_manager_reason_and_closed_run(self):
        self.finish_all(); self.close()
        url = self.u("run-reopen", self.run_id)
        self.assertEqual(self.as_(self.boy).post(url, {"reason": "x"}, format="json").status_code, 403)
        self.assertEqual(self.as_(self.manager).post(url, {}, format="json").status_code, 400)
        self.assertEqual(self.c.post(url, {"reason": "ok"}, format="json").status_code, 200)
        self.assertEqual(self.c.post(url, {"reason": "again"}, format="json").status_code, 400)

    def test_cancelling_only_delivery_reopens_the_visit(self):
        self.entry(self.boy, self.cust[0], items=self.basket())
        d = Delivery.objects.get()
        cancel_delivery(business_id=self.business.pk, delivery_id=d.pk, user=self.owner, reason="Wrong customer")
        self.assertEqual(RouteVisit.objects.get(run_id=self.run_id, customer=self.cust[0]).status, "PENDING")
        self.assertEqual(self.close().status_code, 400)


class RouteTenantTests(Base, TestCase):
    def setUp(self):
        self.make()
        self.setup_route()
        self.start()
        self.hacker = User.objects.create_user(username="r_hack", password="x-pass-123")
        self.other = create_business(name="H", slug="h-dairy", owner=self.hacker)
        self.c = self.as_(self.hacker)

    def test_foreign_ids_through_own_business_are_404(self):
        for call in (
            lambda: self.c.get(self.u("run-tasks", self.run_id, business=self.other)),
            lambda: self.c.post(self.u("run-close", self.run_id, business=self.other), {}, format="json"),
            lambda: self.c.post(self.u("run-reopen", self.run_id, business=self.other), {"reason": "x"}, format="json"),
            lambda: self.c.put(self.u("route-customers", self.route_id, business=self.other), {"customers": []}, format="json"),
            lambda: self.c.post(self.u("run-list-start", business=self.other), {"route_id": self.route_id}, format="json"),
        ):
            self.assertEqual(call().status_code, 404)

    def test_foreign_business_url_is_403(self):
        for name in ("route-list-create", "run-list-start", "standing-order-list-create"):
            self.assertEqual(self.c.get(self.u(name)).status_code, 403)
        self.assertEqual(self.c.get(self.u("run-tasks", self.run_id)).status_code, 403)

    def test_foreign_product_in_entry_is_rejected(self):
        foreign = Product.objects.create(business=self.other, sku="F", name="F")
        r = self.entry(self.manager, self.cust[0], items=[{"product_id": str(foreign.pk), "quantity": "1"}])
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Delivery.objects.exists())

    def test_unauthenticated(self):
        self.c.force_authenticate(None)
        self.assertIn(self.c.get(self.u("route-list-create")).status_code, (401, 403))


class RoutePerformanceTests(Base, TestCase):
    def test_tasks_query_count_is_bounded(self):
        self.make()
        for i in range(3, 60):
            Customer.objects.create(business=self.business, customer_code=f"C{i}", name=f"Cust{i}")
        r = self.as_(self.manager).post(self.u("route-list-create"), {"name": "Big", "assigned_to": str(self.m_boy.pk)}, format="json")
        self.route_id = r.data["id"]
        ids = [str(c.pk) for c in Customer.objects.filter(business=self.business).order_by("customer_code")]
        self.c.put(self.u("route-customers", self.route_id), {"customers": ids}, format="json")
        self.start()
        url = self.u("run-tasks", self.run_id)
        self.as_(self.boy)
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as q:
            resp = self.c.get(url)
        self.assertEqual(len(resp.data["tasks"]), 60)
        self.assertLess(len(q), 15)


class RunConcurrencyTests(Base, TransactionTestCase):
    reset_sequences = True

    def test_two_users_recording_same_visit_deliver_once(self):
        self.make(); self.setup_route(); self.start()
        barrier = Barrier(2)

        def attempt(user_pk):
            close_old_connections()
            try:
                c = APIClient(); c.force_authenticate(User.objects.get(pk=user_pk))
                barrier.wait(timeout=10)
                return c.post(self.u("run-entry", self.run_id), {
                    "customer_id": str(self.cust[0].pk), "outcome": "DELIVERED", "items": self.basket(),
                }, format="json").status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = sorted(pool.map(attempt, [self.boy.pk, self.manager.pk]))

        self.assertEqual(codes, [200, 400])
        self.assertEqual(Delivery.objects.count(), 1)
        self.assertEqual(StockMovement.objects.filter(movement_type="DELIVERY").count(), 1)
