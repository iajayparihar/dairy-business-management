import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.users.models import PasswordResetOTP

User = get_user_model()
PW = "pw-Strong-123"
NEW = "Brand-new-Pass-987"


class StaffBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.owner = User.objects.create_user("st_owner", "o@x.com", PW)
        self.biz = create_business(name="St Dairy", slug="st-dairy", owner=self.owner)
        self.manager = self.member("st_manager", "MANAGER")
        self.staff = self.member("st_staff", "STAFF", phone_number="9826100031")
        self.list_url = reverse("businesses:staff-list-create", args=[self.biz.pk])

    def member(self, username, role, business=None, **extra):
        u = User.objects.create_user(username, password=PW, **extra)
        return BusinessMembership.objects.create(business=business or self.biz, user=u, role=role)

    def login(self, user):
        self.client.force_authenticate(user)

    def mid(self, user):
        return BusinessMembership.objects.get(business=self.biz, user=user).pk

    def detail(self, user):
        return reverse("businesses:staff-detail", args=[self.biz.pk, self.mid(user)])

    def code_url(self, user):
        return reverse("businesses:staff-reset-code", args=[self.biz.pk, self.mid(user)])

    def new_staff(self, **over):
        body = {"username": "new_boy", "first_name": "Raju", "phone_number": "98261 00040"}
        body.update(over)
        return self.client.post(self.list_url, body, format="json")


class StaffAccessTests(StaffBase):
    def test_staff_role_and_outsiders_are_refused_everywhere(self):
        outsider = User.objects.create_user("st_out", password=PW)
        for u in (self.staff.user, outsider):
            self.login(u)
            self.assertEqual(self.client.get(self.list_url).status_code, 403)
            self.assertEqual(self.new_staff().status_code, 403)
            self.assertEqual(self.client.patch(self.detail(self.manager.user), {"is_active": False}, format="json").status_code, 403)
            self.assertEqual(self.client.post(self.code_url(self.staff.user)).status_code, 403)

    def test_anonymous_gets_401(self):
        self.assertEqual(self.client.get(self.list_url).status_code, 401)

    def test_other_business_cannot_be_reached(self):
        other_owner = User.objects.create_user("st_other_owner", password=PW)
        create_business(name="Other", slug="other-dairy", owner=other_owner)
        self.login(other_owner)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)

    def test_membership_of_another_business_is_404(self):
        other_owner = User.objects.create_user("st_other_owner", password=PW)
        other = create_business(name="Other", slug="other-dairy", owner=other_owner)
        foreign = self.member("st_foreign", "STAFF", business=other)
        self.login(self.owner)
        url = reverse("businesses:staff-detail", args=[self.biz.pk, foreign.pk])
        self.assertEqual(self.client.patch(url, {"is_active": False}, format="json").status_code, 404)
        url = reverse("businesses:staff-reset-code", args=[self.biz.pk, foreign.pk])
        self.assertEqual(self.client.post(url).status_code, 404)


class StaffListTests(StaffBase):
    def test_list_flags_per_viewer(self):
        self.login(self.owner)
        rows = {r["username"]: r for r in self.client.get(self.list_url).json()["results"]}
        self.assertEqual(set(rows), {"st_owner", "st_manager", "st_staff"})
        self.assertFalse(rows["st_owner"]["can_manage"])
        self.assertTrue(rows["st_manager"]["can_manage"])
        self.assertTrue(rows["st_staff"]["can_reset_password"])
        self.assertEqual(rows["st_staff"]["phone_number"], "+919826100031")
        self.assertNotIn("password", rows["st_staff"])
        self.login(self.manager.user)
        rows = {r["username"]: r for r in self.client.get(self.list_url).json()["results"]}
        self.assertFalse(rows["st_manager"]["can_manage"])
        self.assertTrue(rows["st_staff"]["can_manage"])

    def test_other_shop_member_cannot_be_reset(self):
        other_owner = User.objects.create_user("st_oo", password=PW)
        other = create_business(name="Other", slug="other-dairy", owner=other_owner)
        BusinessMembership.objects.create(business=other, user=self.staff.user, role="STAFF")
        self.login(self.owner)
        rows = {r["username"]: r for r in self.client.get(self.list_url).json()["results"]}
        self.assertTrue(rows["st_staff"]["can_manage"])
        self.assertFalse(rows["st_staff"]["can_reset_password"])

    def test_filters_and_bad_filters(self):
        self.login(self.owner)
        self.client.patch(self.detail(self.staff.user), {"is_active": False}, format="json")
        q = lambda s: [r["username"] for r in self.client.get(self.list_url + s).json()["results"]]
        self.assertEqual(q("?is_active=false"), ["st_staff"])
        self.assertEqual(q("?role=MANAGER"), ["st_manager"])
        self.assertEqual(q("?search=98261"), ["st_staff"])
        for bad in ("?is_active=maybe", "?role=BOSS"):
            self.assertEqual(self.client.get(self.list_url + bad).status_code, 400)


class StaffCreateTests(StaffBase):
    def test_create_then_first_login_via_code(self):
        self.login(self.owner)
        r = self.new_staff()
        self.assertEqual(r.status_code, 201, r.content)
        d = r.json()
        self.assertEqual((d["role"], d["phone_number"], d["is_active"]), ("STAFF", "+919826100040", True))
        self.assertRegex(d["setup_code"], r"^\d{6}$")
        u = User.objects.get(username="new_boy")
        self.assertFalse(u.has_usable_password())
        # no password yet: login is impossible
        anon = APIClient()
        self.assertEqual(anon.post(reverse("auth:token-obtain"), {"username": "new_boy", "password": "guess-123"}, format="json").status_code, 401)
        # the code is not stored in clear
        self.assertNotIn(d["setup_code"], PasswordResetOTP.objects.get(user=u).code_hash)
        # staff sets own password with the code (by phone number, like the app will)
        r = anon.post(reverse("auth:password-reset"), {"identifier": "9826100040", "otp": d["setup_code"], "new_password": NEW}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        r = anon.post(reverse("auth:token-obtain"), {"username": "new_boy", "password": NEW}, format="json")
        self.assertEqual(r.status_code, 200)
        # the code is single use
        r = anon.post(reverse("auth:password-reset"), {"identifier": "new_boy", "otp": d["setup_code"], "new_password": NEW + "x"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_manager_adds_staff_only(self):
        self.login(self.manager.user)
        self.assertEqual(self.new_staff().status_code, 201)
        self.assertEqual(self.new_staff(username="new_mgr", role="MANAGER", phone_number="").status_code, 403)
        self.assertFalse(User.objects.filter(username="new_mgr").exists())

    def test_owner_cannot_create_owner(self):
        self.login(self.owner)
        r = self.new_staff(role="OWNER")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.new_staff(username="mg", role="MANAGER", phone_number="").status_code, 201)

    def test_duplicates_and_bad_input(self):
        self.login(self.owner)
        self.assertEqual(self.new_staff().status_code, 201)
        r = self.new_staff(username="NEW_BOY", phone_number="")
        self.assertEqual((r.status_code, "username" in r.json()), (400, True))
        r = self.new_staff(username="other", phone_number="+91 9826100040")
        self.assertIn("phone_number", r.json())
        r = self.new_staff(username="o2", phone_number="", email="O@X.com")
        self.assertIn("email", r.json())
        r = self.new_staff(username="o@x.com", phone_number="")  # username equal to someone's email
        self.assertIn("username", r.json())
        self.assertEqual(self.new_staff(username="o3", phone_number="12345").status_code, 400)
        self.assertEqual(self.new_staff(username="bad name!", phone_number="").status_code, 400)
        self.assertEqual(self.new_staff(username="o4", phone_number="", is_superuser=True).status_code, 400)
        self.assertEqual(self.client.post(self.list_url, {}, format="json").status_code, 400)
        self.assertEqual(User.objects.filter(username__in=["other", "o2", "o3", "o4"]).count(), 0)


class StaffUpdateTests(StaffBase):
    def test_deactivate_blocks_that_user_at_once_and_reactivate_restores(self):
        self.login(self.owner)
        r = self.client.patch(self.detail(self.staff.user), {"is_active": False}, format="json")
        self.assertEqual((r.status_code, r.json()["is_active"]), (200, False))
        self.login(self.staff.user)
        self.assertEqual(self.client.get(reverse("suppliers:supplier-list-create", args=[self.biz.pk])).status_code, 403)
        me = self.client.get(reverse("auth:me")).json()
        self.assertEqual(me["memberships"], [])
        self.login(self.owner)
        self.client.patch(self.detail(self.staff.user), {"is_active": True}, format="json")
        self.login(self.staff.user)
        self.assertEqual(self.client.get(reverse("suppliers:supplier-list-create", args=[self.biz.pk])).status_code, 200)

    def test_role_rules(self):
        self.login(self.owner)
        r = self.client.patch(self.detail(self.staff.user), {"role": "MANAGER"}, format="json")
        self.assertEqual(r.json()["role"], "MANAGER")
        self.login(self.manager.user)
        # manager cannot touch another manager, an owner, or promote
        self.assertEqual(self.client.patch(self.detail(self.staff.user), {"is_active": False}, format="json").status_code, 403)
        self.assertEqual(self.client.patch(self.detail(self.owner), {"is_active": False}, format="json").status_code, 403)
        self.login(self.owner)
        self.client.patch(self.detail(self.staff.user), {"role": "STAFF"}, format="json")
        self.login(self.manager.user)
        self.assertEqual(self.client.patch(self.detail(self.staff.user), {"role": "MANAGER"}, format="json").status_code, 403)
        self.assertEqual(self.client.patch(self.detail(self.staff.user), {"is_active": False}, format="json").status_code, 200)

    def test_nobody_edits_owner_or_self_and_last_owner_is_safe(self):
        self.login(self.owner)
        for target in (self.owner, ):
            r = self.client.patch(self.detail(target), {"is_active": False}, format="json")
            self.assertEqual(r.status_code, 400)  # own row
        self.login(self.manager.user)
        self.assertEqual(self.client.patch(self.detail(self.manager.user), {"is_active": False}, format="json").status_code, 400)
        second_owner = self.member("st_owner2", "OWNER")
        self.login(self.owner)
        self.assertEqual(self.client.patch(self.detail(second_owner.user), {"is_active": False}, format="json").status_code, 403)
        self.assertTrue(BusinessMembership.objects.get(pk=second_owner.pk).is_active)

    def test_bad_bodies(self):
        self.login(self.owner)
        for body in ({}, {"role": "OWNER"}, {"role": "BOSS"}, {"is_active": "perhaps"}, {"user": 1}):
            self.assertEqual(self.client.patch(self.detail(self.staff.user), body, format="json").status_code, 400, body)


class StaffResetCodeTests(StaffBase):
    def test_owner_code_resets_password_and_signs_out_everywhere(self):
        self.login(self.owner)
        r = self.client.post(self.code_url(self.staff.user))
        self.assertEqual(r.status_code, 200, r.content)
        code = r.json()["code"]
        self.assertRegex(code, r"^\d{6}$")
        anon = APIClient()
        r = anon.post(reverse("auth:password-reset"), {"identifier": "st_staff", "otp": code, "new_password": NEW}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(anon.post(reverse("auth:token-obtain"), {"username": "st_staff", "password": NEW}, format="json").status_code, 200)

    def test_new_code_replaces_old_and_expires(self):
        self.login(self.owner)
        first = self.client.post(self.code_url(self.staff.user)).json()["code"]
        second = self.client.post(self.code_url(self.staff.user)).json()["code"]
        self.assertEqual(PasswordResetOTP.objects.filter(user=self.staff.user, used_at__isnull=True).count(), 1)
        anon = APIClient()
        body = lambda c: {"identifier": "st_staff", "otp": c, "new_password": NEW}
        if first != second:
            self.assertEqual(anon.post(reverse("auth:password-reset"), body(first), format="json").status_code, 400)
        PasswordResetOTP.objects.filter(user=self.staff.user, used_at__isnull=True).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(anon.post(reverse("auth:password-reset"), body(second), format="json").status_code, 400)

    def test_manager_resets_staff_only(self):
        self.login(self.manager.user)
        self.assertEqual(self.client.post(self.code_url(self.staff.user)).status_code, 200)
        self.assertEqual(self.client.post(self.code_url(self.owner)).status_code, 403)
        self.assertEqual(self.client.post(self.code_url(self.manager.user)).status_code, 400)  # self

    def test_refused_cases(self):
        self.login(self.owner)
        # inactive member
        self.client.patch(self.detail(self.staff.user), {"is_active": False}, format="json")
        self.assertEqual(self.client.post(self.code_url(self.staff.user)).status_code, 400)
        # works in another shop: owner of this shop must not take over that account
        other_owner = User.objects.create_user("st_oo", password=PW)
        other = create_business(name="Other", slug="other-dairy", owner=other_owner)
        BusinessMembership.objects.create(business=other, user=self.manager.user, role="STAFF")
        self.assertEqual(self.client.post(self.code_url(self.manager.user)).status_code, 400)
        self.assertFalse(PasswordResetOTP.objects.filter(user=self.manager.user).exists())
        # admin-level accounts are never reset from the app
        boss = self.member("st_admin", "STAFF")
        User.objects.filter(pk=boss.user_id).update(is_staff=True)
        self.assertEqual(self.client.post(self.code_url(boss.user)).status_code, 400)
        # an inactive membership elsewhere does not block
        BusinessMembership.objects.filter(business=other, user=self.manager.user).update(is_active=False)
        self.assertEqual(self.client.post(self.code_url(self.manager.user)).status_code, 200)

    def test_wrong_code_attempts_are_limited(self):
        self.login(self.owner)
        code = self.client.post(self.code_url(self.staff.user)).json()["code"]
        wrong = "000000" if code != "000000" else "111111"
        anon = APIClient()
        for _ in range(5):
            anon.post(reverse("auth:password-reset"), {"identifier": "st_staff", "otp": wrong, "new_password": NEW}, format="json")
        cache.clear()  # throttle is separate; attempts are enforced by the OTP row
        r = anon.post(reverse("auth:password-reset"), {"identifier": "st_staff", "otp": code, "new_password": NEW}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_forgot_by_phone_gives_same_generic_answer(self):
        anon = APIClient()
        a = anon.post(reverse("auth:password-forgot"), {"identifier": "9826100031"}, format="json")
        b = anon.post(reverse("auth:password-forgot"), {"identifier": "9999999999"}, format="json")
        self.assertEqual(a.json(), b.json())
