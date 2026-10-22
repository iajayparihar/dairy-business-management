"""Owner-only milk rate correction: preview, confirm, audit and money."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.payments.selectors import get_supplier_balance
from apps.purchases.models import MilkCollection
from apps.suppliers.models import SupplierMilkRate
from apps.suppliers.services import resolve_supplier_milk_rate
from tests.purchases.test_milk_api_qa import Base

FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


@FAST
class RateCorrectionQA(Base, TestCase):
    def setUp(self):
        super().setUp()
        self.url_c = f"{self.base}suppliers/{self.sup.pk}/milk-rates/correct/"
        self.day = lambda n, h=6: (timezone.now() - timedelta(days=n)).replace(hour=h, minute=0, second=0, microsecond=0)
        # 1 litre-day entries: 20 days ago, 9, 8, 5 days ago (two in the window), 1 day ago
        self.ids = {n: self.post(collected_at=self.day(n).isoformat(), litres="10", fat="9").json()["id"] for n in (20, 9, 8, 5, 1)}
        self.window = {"effective_from": self.day(10).isoformat(), "effective_to": self.day(3).isoformat()}  # covers 9, 8, 5

    def cbody(self, **kw):
        b = {"milk_type": "BUFFALO", **self.window, "rate": "16.00", "reason": "Rate was 16 from the start"}
        b.update(kw)
        return b

    def call(self, user=None, **kw):
        return self.api(user or self.owner).post(self.url_c, self.cbody(**kw), format="json")

    def confirm(self, **kw):
        p = self.call(**kw).json()
        return self.call(confirm=True, expected_count=p["count"], expected_new_total=p["new_total"], **kw)

    def test_preview_writes_nothing_and_shows_old_and_new(self):
        before = (SupplierMilkRate.objects.count(), MilkCollection.objects.count())
        r = self.call()
        self.assertEqual(r.status_code, 200, r.content)
        j = r.json()
        self.assertEqual((j["confirmed"], j["count"]), (False, 3))
        self.assertEqual((j["old_total"], j["new_total"], j["difference"]), ("4050.00", "4320.00", "270.00"))
        self.assertEqual(j["entries"][0]["old_amount"], "1350.00")
        self.assertEqual(j["entries"][0]["new_amount"], "1440.00")
        self.assertEqual(len(j["rates_cancelled"]), 1)
        self.assertEqual((SupplierMilkRate.objects.count(), MilkCollection.objects.count()), before)

    def test_only_the_owner_can_correct(self):
        for u in (self.manager, self.staff, self.outsider):
            self.assertEqual(self.call(u).status_code, 403)

    def test_confirm_cancels_old_rate_splits_the_period_and_reprices_entries(self):
        r = self.confirm()
        self.assertEqual(r.status_code, 200, r.content)
        j = r.json()
        self.assertEqual((j["confirmed"], len(j["corrected"])), (True, 3))
        old = SupplierMilkRate.objects.get(rate="15.00", cancelled_at__isnull=False)
        self.assertIn("Rate corrected", old.cancel_reason)
        self.assertEqual(old.cancelled_by_id, self.owner.pk)
        live = list(SupplierMilkRate.objects.filter(milk_type="BUFFALO", cancelled_at__isnull=True).order_by("effective_from"))
        self.assertEqual([(str(x.rate), x.effective_to is None) for x in live], [("15.00", False), ("16.00", False), ("15.00", True)])
        at = lambda d: str(resolve_supplier_milk_rate(business_id=self.biz.pk, supplier_id=self.sup.pk, milk_type="BUFFALO", collected_at=d).rate)
        self.assertEqual((at(self.day(20)), at(self.day(8)), at(self.day(1))), ("15.00", "16.00", "15.00"))

    def test_entries_are_cancelled_and_replaced_at_their_original_time(self):
        self.confirm()
        for n in (9, 8, 5):
            old = MilkCollection.objects.get(pk=self.ids[n])
            self.assertEqual(old.status, "CANCELLED")
            new = MilkCollection.objects.get(replaces=old)
            self.assertEqual((new.collected_at, new.shift, new.litres, new.fat), (old.collected_at, old.shift, old.litres, old.fat))
            self.assertEqual((new.applied_rate, new.amount), (Decimal("16.00"), Decimal("1440.00")))
            self.assertNotEqual(new.collection_number, old.collection_number)
        for n in (20, 1):
            c = MilkCollection.objects.get(pk=self.ids[n])
            self.assertEqual((c.status, c.applied_rate), ("RECORDED", Decimal("15.00")))

    def test_supplier_balance_follows_the_difference(self):
        before = get_supplier_balance(business_id=self.biz.pk, supplier_id=self.sup.pk)["outstanding_amount"]
        self.confirm()
        after = get_supplier_balance(business_id=self.biz.pk, supplier_id=self.sup.pk)["outstanding_amount"]
        self.assertEqual(after - before, Decimal("270.00"))

    def test_rate_history_shows_the_cancelled_row_with_who_and_why(self):
        self.confirm()
        rows = self.api(self.owner).get(f"{self.base}suppliers/{self.sup.pk}/milk-rates/", {"page_size": 50}).json()["results"]
        cancelled = [r for r in rows if r["cancelled_at"]]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual((cancelled[0]["rate"], cancelled[0]["cancelled_by"] is not None), ("15.00", True))
        self.assertIn("Rate was 16", cancelled[0]["cancel_reason"])

    def test_confirm_needs_the_previewed_numbers_and_stale_preview_is_refused(self):
        self.assertEqual(self.call(confirm=True).status_code, 400)
        p = self.call().json()
        self.assertEqual(self.call(confirm=True, expected_count=p["count"] + 1, expected_new_total=p["new_total"]).status_code, 400)
        self.assertEqual(self.call(confirm=True, expected_count=p["count"], expected_new_total="1.00").status_code, 400)
        self.post(collected_at=self.day(6).isoformat(), litres="3", fat="8")  # a new entry after the preview
        self.assertEqual(self.call(confirm=True, expected_count=p["count"], expected_new_total=p["new_total"]).status_code, 400)
        self.assertEqual(SupplierMilkRate.objects.filter(cancelled_at__isnull=False).count(), 0)

    def test_validation(self):
        bad = [dict(reason=""), dict(reason="  "), dict(reason="x" * 501), dict(rate="0"), dict(rate="-1"), dict(rate="abc"),
               dict(rate="1.234"), dict(milk_type="GOAT"), dict(effective_from="2026-01-01T10:00:00"), dict(effective_from="junk"),
               dict(effective_from=self.window["effective_to"], effective_to=self.window["effective_from"]),
               dict(effective_from=self.day(40).isoformat(), effective_to=self.day(3).isoformat()),   # more than 31 days
               dict(effective_to=(timezone.now() + timedelta(days=2)).isoformat()),                   # not yet over
               dict(bogus=1)]
        for kw in bad:
            self.assertEqual(self.call(**kw).status_code, 400, str(kw)[:70])
        self.assertEqual(self.call(milk_type="COW").json()["count"], 0)

    def test_unknown_or_foreign_supplier_is_404(self):
        self.assertEqual(self.api(self.owner).post(f"{self.base}suppliers/11111111-1111-1111-1111-111111111111/milk-rates/correct/",
                                                   self.cbody(), format="json").status_code, 404)

    def test_cancelled_entries_and_other_suppliers_are_left_alone(self):
        self.api(self.owner).post(f"{self.url}{self.ids[8]}/cancel/", {"reason": "wrong can"}, format="json")
        self.assertEqual(self.call().json()["count"], 2)

    def test_correcting_twice_is_clean(self):
        self.confirm()
        r = self.confirm(rate="17.00", reason="17 was right")
        self.assertEqual(r.status_code, 200, r.content)
        live = SupplierMilkRate.objects.filter(milk_type="BUFFALO", cancelled_at__isnull=True)
        self.assertEqual(sorted(str(x.rate) for x in live), ["15.00", "15.00", "17.00"])
        self.assertEqual(MilkCollection.objects.filter(status="RECORDED", applied_rate="17.00").count(), 3)
