from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.users.phone import normalize_phone

User = get_user_model()


class PhoneNormalizeTests(SimpleTestCase):
    def test_accepted_forms(self):
        for raw in ["9826100002", "+91 98261 00002", "919826100002", "09826100002", " 98261-00002 "]:
            self.assertEqual(normalize_phone(raw), "+919826100002", raw)
        self.assertEqual(normalize_phone(""), "")
        self.assertEqual(normalize_phone(None), "")

    def test_rejected_forms(self):
        for raw in ["12345", "5826100002", "+14155550100", "98261000021", "98261x0002", "++919826100002", "９８２６１００００２"]:
            with self.assertRaises(ValueError, msg=raw):
                normalize_phone(raw)


class UserPhoneModelTests(TestCase):
    def test_save_normalizes_and_rejects_invalid(self):
        u = User.objects.create_user("p1", phone_number="9826100002")
        self.assertEqual(u.phone_number, "+919826100002")
        u.phone_number = "123"
        with self.assertRaises(ValidationError):
            u.save()

    def test_login_style_partial_save_never_fails_on_legacy_value(self):
        u = User.objects.create_user("p2", phone_number="9826100003")
        User.objects.filter(pk=u.pk).update(phone_number="legacy")
        u.refresh_from_db()
        u.save(update_fields=["last_login"])  # what login does

    def test_phone_unique_when_set_blank_repeats(self):
        User.objects.create_user("a1", phone_number="9826100004")
        User.objects.create_user("a2")
        User.objects.create_user("a3")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("a4", phone_number="+91 98261 00004")

    def test_username_unique_ignoring_case(self):
        User.objects.create_user("Ramesh")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("ramesh")


class MeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.url = reverse("auth:me")
        self.user = User.objects.create_user("me_u", "me@x.com", "pw-Strong-123", first_name="Me", phone_number="9826100020")
        self.a = create_business(name="A Dairy", slug="a-dairy", owner=self.user, bill_language="hi")
        owner_b = User.objects.create_user("me_b_owner", password="pw-Strong-123")
        self.b = create_business(name="B Dairy", slug="b-dairy", owner=owner_b)
        BusinessMembership.objects.create(business=self.b, user=self.user, role="MANAGER")

    def test_requires_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_user_and_memberships(self):
        self.client.force_authenticate(self.user)
        d = self.client.get(self.url).json()
        self.assertEqual(d["username"], "me_u")
        self.assertEqual(d["phone_number"], "+919826100020")
        self.assertNotIn("password", d)
        self.assertEqual(
            [(m["business_name"], m["role"]) for m in d["memberships"]],
            [("A Dairy", "OWNER"), ("B Dairy", "MANAGER")],
        )
        self.assertEqual(d["memberships"][0]["bill_language"], "hi")
        self.assertEqual(d["memberships"][0]["timezone"], "Asia/Kolkata")

    def test_hides_inactive_membership_and_inactive_or_deleted_business(self):
        self.client.force_authenticate(self.user)
        BusinessMembership.objects.filter(business=self.b, user=self.user).update(is_active=False)
        names = [m["business_name"] for m in self.client.get(self.url).json()["memberships"]]
        self.assertEqual(names, ["A Dairy"])
        self.a.is_active = False
        self.a.save()
        self.assertEqual(self.client.get(self.url).json()["memberships"], [])

    def test_no_membership_and_superuser_get_empty_list(self):
        for u in [User.objects.create_user("lonely"), User.objects.create_superuser("root", "r@x.com", "pw-Strong-123")]:
            self.client.force_authenticate(u)
            self.assertEqual(self.client.get(self.url).json()["memberships"], [])

    def test_only_own_memberships(self):
        other = User.objects.get(username="me_b_owner")
        self.client.force_authenticate(other)
        ids = [m["business_id"] for m in self.client.get(self.url).json()["memberships"]]
        self.assertEqual(ids, [str(self.b.pk)])
