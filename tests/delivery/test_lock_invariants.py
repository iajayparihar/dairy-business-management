"""
Race tests that FORCE the bad interleaving.

A plain "start two threads together" test can pass with no locking at all
(the threads simply don't overlap). Here each test pauses one transaction
inside its critical window, lets the rival run, and then asserts the
invariant. Remove the matching lock and the test fails: verified by hand
when this file was written.

The gate waits only briefly. With the lock in place the rival blocks, the
gate times out, and the paused transaction continues, which is the
serialized outcome the invariant allows.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import StringIO
from threading import Barrier, BrokenBarrierError, Event
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from apps.deliveries import services
from apps.deliveries.models import Delivery
from apps.deliveries.services import (
    cancel_delivery,
    correct_delivery,
    create_delivery,
)
from apps.inventory.models import StockMovement
from apps.routes.models import RouteRun, RouteVisit
from apps.routes.services import close_run
from tests.routes.test_routes import Base

User = get_user_model()
GATE = 1.0  # seconds a paused transaction waits for its rival


def in_thread(fn):
    """Run fn with its own DB connection; return ('ok', value) or the error."""
    close_old_connections()
    try:
        return ("ok", fn())
    except ValidationError as exc:
        return ("rejected", exc)
    finally:
        close_old_connections()


class LockInvariantTests(Base, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.make()
        self.setup_route()
        self.start()
        StockMovement.objects.create(
            business=self.business, product=self.cow,
            movement_type=StockMovement.MovementType.ADJUSTMENT_IN,
            quantity=Decimal("3"), created_by=self.owner,
            occurred_at=self.run_obj().created_at,
        )

    def run_obj(self):
        return RouteRun.objects.get(pk=self.run_id)

    def meet(self, original):
        """Wrap `original` so two callers pause together before returning."""
        barrier = Barrier(2)

        def wrapper(*a, **kw):
            result = original(*a, **kw)
            try:
                barrier.wait(timeout=GATE)
            except BrokenBarrierError:
                pass  # the rival is blocked by a lock: serialized, fine
            return result

        return wrapper

    def deliver(self, customer, qty="1", user=None):
        return create_delivery(
            business_id=self.business.pk, customer_id=customer.pk,
            product_id=self.cow.pk, quantity=qty, user=user or self.owner,
        )

    # -- uniqueness: the product lock serializes the duplicate check

    def test_duplicate_shift_delivery_is_refused_cleanly_not_by_db_error(self):
        # Both transactions reach pricing; only the lock can keep the second
        # one from passing the duplicate check too.
        gated = self.meet(services._effective_selling_price)
        with mock.patch.object(services, "_effective_selling_price", gated):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda _: in_thread(lambda: self.deliver(self.cust[0])),
                    range(2),
                ))

        self.assertEqual(sorted(r[0] for r in results), ["ok", "rejected"])
        self.assertEqual(Delivery.objects.count(), 1)
        self.assertEqual(
            StockMovement.objects.filter(movement_type="DELIVERY").count(), 1,
        )

    def test_client_ref_reused_across_products_is_refused_not_a_500(self):
        # Different products take different locks, so nothing serializes
        # them: the unique constraint is the guard, and must read as a 400.
        gated = self.meet(services._effective_selling_price)

        def attempt(product):
            return in_thread(lambda: create_delivery(
                business_id=self.business.pk, customer_id=self.cust[0].pk,
                product_id=product.pk, quantity="1", user=self.owner,
                client_ref="reused",
            ))

        with mock.patch.object(services, "_effective_selling_price", gated):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, [self.cow, self.dahi]))

        self.assertEqual(sorted(r[0] for r in results), ["ok", "rejected"])
        self.assertEqual(Delivery.objects.count(), 1)

    # -- stock: the product lock keeps the shortfall honest

    def test_concurrent_deliveries_never_both_see_the_same_stock(self):
        # Stock is 3; two customers take 2 each. Both read the balance, then
        # meet. Without the product lock both see 3 and report no shortfall.
        gated = self.meet(services._get_stock_balance)
        customers = [self.cust[0], self.cust[1]]
        with mock.patch.object(services, "_get_stock_balance", gated):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda c: in_thread(lambda: self.deliver(c, qty="2")),
                    customers,
                ))

        self.assertEqual([r[0] for r in results], ["ok", "ok"])
        shortfall = sum(d.stock_shortfall for d in Delivery.objects.all())
        self.assertEqual(shortfall, Decimal("1.000"))  # 4 out, only 3 in

    # -- closed runs: cancel/correct cannot slip past close_run

    def _delivered_run(self):
        """Every visit resolved; run still open, so close_run would succeed."""
        self.entry(self.boy, self.cust[0], items=self.basket("1"))
        self.entry(self.boy, self.cust[1], "ABSENT")
        self.entry(self.boy, self.cust[2], "NOT_DELIVERED", reason="Refused")
        return Delivery.objects.get(visit__run_id=self.run_id)

    def _race_close_against(self, mutate):
        """
        Pause `mutate` right after it has passed its closed-run check, run
        close_run meanwhile, then let `mutate` continue.
        """
        paused, release, close_done = Event(), Event(), Event()
        real = services._cancel_locked

        def pausing(*a, **kw):
            paused.set()           # the closed-run check has been made
            release.wait(timeout=5)
            return real(*a, **kw)

        def closer():
            try:
                return in_thread(lambda: close_run(
                    business_id=self.business.pk, run_id=self.run_id,
                    user=User.objects.get(pk=self.boy.pk),
                ))
            finally:
                close_done.set()

        with mock.patch.object(services, "_cancel_locked", pausing):
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_mutate = pool.submit(in_thread, mutate)
                self.assertTrue(paused.wait(timeout=5))
                fut_close = pool.submit(closer)
                close_done.wait(timeout=GATE)  # finishes at once if unlocked
                release.set()
                return fut_mutate.result(), fut_close.result()

    def assertRunConsistent(self):
        run = self.run_obj()
        pending = RouteVisit.objects.filter(run=run, status="PENDING").exists()
        self.assertFalse(
            run.status == "CLOSED" and pending,
            "closed run has a visit with no outcome",
        )
        if run.status == "CLOSED":
            for v in run.visits.filter(status="DELIVERED"):
                self.assertTrue(
                    v.deliveries.filter(status="DELIVERED").exists(),
                    "closed run lists a delivered visit with nothing delivered",
                )
            late = Delivery.objects.filter(
                visit__run=run, status="CANCELLED", cancelled_at__gt=run.closed_at,
            )
            self.assertFalse(late.exists(), "delivery changed after the run closed")

    def test_cancel_cannot_race_past_close_run(self):
        d = self._delivered_run()
        mutate = lambda: cancel_delivery(
            business_id=self.business.pk, delivery_id=d.pk,
            user=User.objects.get(pk=self.owner.pk), reason="wrong",
        )
        outcomes = self._race_close_against(mutate)

        self.assertRunConsistent()
        self.assertEqual(
            sorted(o[0] for o in outcomes).count("ok"), 1,
            "exactly one of cancel/close may win",
        )

    def test_correct_cannot_race_past_close_run(self):
        d = self._delivered_run()
        mutate = lambda: correct_delivery(
            business_id=self.business.pk, delivery_id=d.pk, quantity="2",
            user=User.objects.get(pk=self.owner.pk), reason="wrong qty",
        )
        self._race_close_against(mutate)

        # Correct-then-close and close-then-refused-correct are both valid
        # serial orders; a correction landing after the close is not.
        self.assertRunConsistent()


class AuditDeliveriesCommandTests(TransactionTestCase):
    def run_audit(self):
        out = StringIO()
        call_command("audit_deliveries", stdout=out)
        return out.getvalue()

    def test_clean_database_passes(self):
        self.assertIn("safe to migrate", self.run_audit())

    def test_duplicate_is_reported_even_when_constraint_is_absent(self):
        # Simulate legacy data by dropping the constraint, then restore it.
        from django.utils import timezone
        from apps.businesses.services import create_business
        from apps.customers.models import Customer
        from apps.products.models import Product

        owner = User.objects.create_user(username="aud", password="x-pass-123")
        biz = create_business(name="A", slug="aud-dairy", owner=owner)
        cust = Customer.objects.create(business=biz, customer_code="C", name="C")
        prod = Product.objects.create(business=biz, sku="P", name="P")
        with connection.cursor() as cur:
            cur.execute("DROP INDEX uniq_delivery_per_shift")
        try:
            for _ in range(2):
                Delivery.objects.create(
                    business=biz, customer=cust, product=prod, quantity=Decimal('1'),
                    unit_price=Decimal('1'), delivered_at=timezone.now(),
                )
            with self.assertRaises(CommandError):
                self.run_audit()
        finally:
            with connection.cursor() as cur:
                cur.execute("DELETE FROM deliveries_delivery")
                cur.execute(
                    "CREATE UNIQUE INDEX uniq_delivery_per_shift ON deliveries_delivery "
                    "(business_id, customer_id, product_id, delivery_date, shift) "
                    "WHERE status = 'DELIVERED'"
                )
