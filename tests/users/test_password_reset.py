import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.businesses.models import BusinessMembership
from apps.businesses.services import create_business
from apps.users.models import PasswordResetOTP

User = get_user_model()
OLD = "old-password-123!"
NEW = "brand-new-Pass-987"


def otp_from_outbox(index=-1):
    return re.search(r"\b(\d{6})\b", mail.outbox[index].body).group(1)


class PasswordResetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.owner = User.objects.create_user("pwr_owner", "owner@x.com", OLD)
        self.staff = User.objects.create_user("pwr_staff", "staff@x.com", OLD, is_staff=True)
        self.root = User.objects.create_superuser("pwr_root", "root@x.com", OLD)
        self.business = create_business(name="PW Dairy", slug="pw-dairy", owner=self.owner)
        self.forgot = reverse("auth:password-forgot")
        self.reset = reverse("auth:password-reset")
        self.change = reverse("auth:password-change")

    def ask(self, identifier):
        return self.client.post(self.forgot, {"identifier": identifier}, format="json")

    def do_reset(self, identifier, otp, pw=NEW):
        return self.client.post(
            self.reset, {"identifier": identifier, "otp": otp, "new_password": pw}, format="json"
        )

    # forgot
    def test_forgot_response_identical_and_mail_only_for_valid(self):
        User.objects.create_user("pwr_noemail", "", OLD)
        User.objects.create_user("pwr_inactive", "in@x.com", OLD, is_active=False)
        bodies = [
            self.ask(i).json()
            for i in ["pwr_owner", "nobody", "pwr_noemail", "in@x.com"]
        ]
        self.assertEqual(len({str(b) for b in bodies}), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@x.com"])

    def test_works_for_owner_staff_superuser_by_username_or_email(self):
        for ident in ["pwr_owner", "STAFF@x.com", "pwr_root"]:
            self.ask(ident)
        self.assertEqual(len(mail.outbox), 3)

    def test_cooldown_then_new_otp_invalidates_old(self):
        self.ask("pwr_owner")
        self.ask("pwr_owner")
        self.assertEqual(len(mail.outbox), 1)
        first = otp_from_outbox()
        PasswordResetOTP.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self.ask("pwr_owner")
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self.do_reset("pwr_owner", first).status_code, 400)
        self.assertEqual(self.do_reset("pwr_owner", otp_from_outbox()).status_code, 200)

    def test_ambiguous_identifier_sends_nothing(self):
        User.objects.create_user("dup_a", "shared@x.com", OLD)
        User.objects.create_user("dup_b", "SHARED@x.com", OLD)
        self.ask("shared@x.com")
        self.assertEqual(len(mail.outbox), 0)

    def test_email_failure_invalidates_otp_and_is_not_leaked(self):
        with mock.patch("apps.users.services.send_mail", side_effect=OSError("smtp down")):
            r = self.ask("pwr_owner")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PasswordResetOTP.objects.count(), 0)
        self.ask("pwr_owner")  # no cooldown lockout after failure
        self.assertEqual(len(mail.outbox), 1)

    # reset
    def test_reset_success_revokes_only_that_users_tokens(self):
        mine = RefreshToken.for_user(self.owner)
        other = RefreshToken.for_user(self.staff)
        role_before = BusinessMembership.objects.get(user=self.owner).role
        self.ask("pwr_owner")
        code = otp_from_outbox()
        self.assertFalse(PasswordResetOTP.objects.filter(code_hash=code).exists())
        self.assertEqual(self.do_reset("pwr_owner", code).status_code, 200)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(NEW))
        self.assertTrue(BlacklistedToken.objects.filter(token__jti=mine["jti"]).exists())
        self.assertFalse(BlacklistedToken.objects.filter(token__jti=other["jti"]).exists())
        self.assertEqual(BusinessMembership.objects.get(user=self.owner).role, role_before)
        login = self.client.post(
            reverse("auth:token-obtain"), {"username": "pwr_owner", "password": NEW}, format="json"
        )
        self.assertEqual(login.status_code, 200)

    def test_otp_cannot_be_reused(self):
        self.ask("pwr_owner")
        code = otp_from_outbox()
        self.assertEqual(self.do_reset("pwr_owner", code).status_code, 200)
        self.assertEqual(self.do_reset("pwr_owner", code, "another-Pass-4455").status_code, 400)

    def test_wrong_otp_five_times_kills_otp(self):
        self.ask("pwr_owner")
        code = otp_from_outbox()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assertEqual(self.do_reset("pwr_owner", wrong).status_code, 400)
        self.assertEqual(PasswordResetOTP.objects.get().attempts, 5)
        self.assertEqual(self.do_reset("pwr_owner", code).status_code, 400)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(OLD))

    def test_expired_otp(self):
        self.ask("pwr_owner")
        PasswordResetOTP.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.do_reset("pwr_owner", otp_from_outbox()).status_code, 400)

    def test_weak_password_rejected_and_otp_still_usable(self):
        self.ask("pwr_owner")
        code = otp_from_outbox()
        r = self.do_reset("pwr_owner", code, "12345678")
        self.assertEqual(r.status_code, 400)
        self.assertIn("new_password", r.json())
        self.assertEqual(self.do_reset("pwr_owner", code).status_code, 200)

    def test_otp_for_other_user_does_not_work(self):
        self.ask("pwr_owner")
        self.assertEqual(self.do_reset("pwr_staff", otp_from_outbox()).status_code, 400)

    def test_malformed_otp_rejected(self):
        self.assertEqual(self.do_reset("pwr_owner", "abc").status_code, 400)

    # change
    def test_change_password(self):
        self.assertEqual(
            self.client.post(self.change, {"old_password": OLD, "new_password": NEW}).status_code, 401
        )
        refresh = RefreshToken.for_user(self.staff)
        self.client.force_authenticate(self.staff)
        bad = self.client.post(self.change, {"old_password": "nope", "new_password": NEW})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post(self.change, {"old_password": OLD, "new_password": NEW})
        self.assertEqual(ok.status_code, 200)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password(NEW))
        self.assertTrue(BlacklistedToken.objects.filter(token__jti=refresh["jti"]).exists())

    # throttle
    def test_forgot_is_throttled(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"password_forgot": "2/hour"}):
            codes = [self.ask("pwr_owner").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_logout_still_blacklists_refresh(self):
        refresh = RefreshToken.for_user(self.owner)
        r = self.client.post(reverse("auth:token-blacklist"), {"refresh": str(refresh)}, format="json")
        self.assertEqual(r.status_code, 200)

    def test_purge_command(self):
        self.ask("pwr_owner")
        PasswordResetOTP.objects.update(
            expires_at=timezone.now() - timedelta(days=2),
            created_at=timezone.now() - timedelta(days=2),
        )
        from django.core.management import call_command
        call_command("purge_password_otps")
        self.assertEqual(PasswordResetOTP.objects.count(), 0)
