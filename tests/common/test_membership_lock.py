"""update_membership must not block inserts that reference the business."""
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Event
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.businesses.models import BusinessMembership
from apps.businesses.services import update_membership
from apps.customers.models import Customer
from tests.billing.test_concurrency import RaceBase, in_thread


class MembershipLockTests(RaceBase):
    def setUp(self):
        super().setUp()
        self.staff_membership = BusinessMembership.objects.get(
            business=self.business, user=self.staff)

    def test_business_row_is_locked_without_blocking_foreign_key_inserts(self):
        with CaptureQueriesContext(connection) as ctx:
            update_membership(membership_id=self.staff_membership.pk, role="MANAGER")
        locks = [q["sql"] for q in ctx if 'FROM "businesses_business"' in q["sql"] and "FOR" in q["sql"]]
        self.assertTrue(locks)
        self.assertTrue(all("FOR NO KEY UPDATE" in sql for sql in locks), locks)

    def test_inserts_referencing_the_business_are_not_held_up(self):
        paused, release = Event(), Event()
        real_save = BusinessMembership.save

        def pausing(this, *args, **kwargs):
            result = real_save(this, *args, **kwargs)
            paused.set()  # still inside update_membership's transaction
            release.wait(timeout=10)
            return result

        # A plain insert that references the business (FOR KEY SHARE on it).
        # A plain FOR UPDATE on the business row would make this wait.
        sale = lambda: Customer.objects.create(
            business=self.business, customer_code="NEW1", name="New")
        with mock.patch.object(BusinessMembership, "save", pausing):
            with ThreadPoolExecutor(max_workers=2) as pool:
                leader = pool.submit(in_thread, lambda: update_membership(
                    membership_id=self.staff_membership.pk, role="MANAGER"))
                self.assertTrue(paused.wait(timeout=10))
                rival = pool.submit(in_thread, sale)
                try:
                    outcome = rival.result(timeout=5)
                except FutureTimeout:
                    outcome = None
                release.set()
                leader.result()
        self.assertIsNotNone(outcome, "an insert was blocked by update_membership")
        self.assertEqual(outcome[0], "ok")
        self.assertTrue(Customer.objects.filter(customer_code="NEW1").exists())
