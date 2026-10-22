from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import create_business
from apps.suppliers.models import Supplier

User = get_user_model()
PW = "pw-Strong-123"


class ShopDeleteTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.owner = User.objects.create_user("sd_owner", password=PW)
        self.biz = create_business(name="Delete Me Dairy", slug="delete-me", owner=self.owner)
        self.manager = self.member("sd_manager", "MANAGER")
        self.staff = self.member("sd_staff", "STAFF")
        self.url = reverse("businesses:business-detail", args=[self.biz.pk])
        self.restore = reverse("businesses:business-restore", args=[self.biz.pk])
        self.suppliers = reverse("suppliers:supplier-list-create", args=[self.biz.pk])
        Supplier.objects.create(business=self.biz, supplier_code="F1", name="Ramesh")

    def member(self, name, role):
        u = User.objects.create_user(name, password=PW)
        BusinessMembership.objects.create(business=self.biz, user=u, role=role)
        return u

    def delete(self, user, name="Delete Me Dairy"):
        self.client.force_authenticate(user)
        return self.client.delete(self.url, {"confirm_name": name}, format="json")

    def test_only_owner_can_delete(self):
        for u in (self.manager, self.staff, User.objects.create_user("sd_out", password=PW)):
            self.assertEqual(self.delete(u).status_code, 403)
        self.assertFalse(Business.all_objects.get(pk=self.biz.pk).is_deleted)

    def test_name_must_match_and_body_is_strict(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.delete(self.owner, "delete me dairy").status_code, 400)
        self.assertEqual(self.delete(self.owner, "").status_code, 400)
        self.assertEqual(self.client.delete(self.url, {}, format="json").status_code, 400)
        self.assertEqual(self.client.delete(self.url, {"confirm_name": "Delete Me Dairy", "x": 1}, format="json").status_code, 400)
        self.assertFalse(Business.all_objects.get(pk=self.biz.pk).is_deleted)
        self.assertEqual(self.delete(self.owner, "  Delete Me Dairy ").status_code, 204)

    def test_deleted_shop_is_closed_to_everyone_but_data_stays(self):
        self.assertEqual(self.delete(self.owner).status_code, 204)
        b = Business.all_objects.get(pk=self.biz.pk)
        self.assertTrue(b.is_deleted)
        self.assertEqual(b.deleted_by, self.owner)
        for u in (self.owner, self.manager, self.staff):
            self.client.force_authenticate(u)
            self.assertEqual(self.client.get(self.suppliers).status_code, 403)
            self.assertEqual(self.client.get(reverse("auth:me")).json()["memberships"], [])
        self.assertEqual(self.delete(self.owner).status_code, 403)  # not reachable twice
        self.assertTrue(Supplier.objects.filter(business=self.biz).exists())

    def test_me_lists_deleted_shops_for_the_owner_only(self):
        self.delete(self.owner)
        self.client.force_authenticate(self.owner)
        d = self.client.get(reverse("auth:me")).json()
        self.assertEqual([s["business_name"] for s in d["deleted_shops"]], ["Delete Me Dairy"])
        for u in (self.manager, self.staff):
            self.client.force_authenticate(u)
            self.assertEqual(self.client.get(reverse("auth:me")).json()["deleted_shops"], [])

    def test_only_owner_restores(self):
        self.delete(self.owner)
        stranger = User.objects.create_user("sd_out2", password=PW)
        for u in (self.manager, self.staff, stranger):
            self.client.force_authenticate(u)
            self.assertEqual(self.client.post(self.restore).status_code, 404)
        self.assertTrue(Business.all_objects.get(pk=self.biz.pk).is_deleted)
        self.client.force_authenticate(self.owner)
        r = self.client.post(self.restore)
        self.assertEqual((r.status_code, r.json()["business_name"]), (200, "Delete Me Dairy"))
        b = Business.all_objects.get(pk=self.biz.pk)
        self.assertEqual((b.is_deleted, b.deleted_at, b.deleted_by), (False, None, None))
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(self.suppliers).status_code, 200)
        self.assertEqual(Supplier.objects.filter(business=self.biz).count(), 1)

    def test_restore_of_live_or_unknown_shop(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.post(self.restore).status_code, 400)
        import uuid
        self.assertEqual(self.client.post(reverse("businesses:business-restore", args=[uuid.uuid4()])).status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(self.restore).status_code, 401)

    def test_restore_does_not_reactivate_a_suspended_shop(self):
        self.delete(self.owner)
        Business.all_objects.filter(pk=self.biz.pk).update(is_active=False)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.post(self.restore).status_code, 200)
        self.assertFalse(Business.objects.get(pk=self.biz.pk).is_active)
        self.assertEqual(self.client.get(self.suppliers).status_code, 403)

    def test_owner_of_another_shop_cannot_restore_this_one(self):
        self.delete(self.owner)
        other = User.objects.create_user("sd_other_owner", password=PW)
        create_business(name="Other", slug="other-sd", owner=other)
        self.client.force_authenticate(other)
        self.assertEqual(self.client.post(self.restore).status_code, 404)
