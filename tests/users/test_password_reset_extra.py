import threading
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.users import services
from apps.users.models import PasswordResetOTP
from apps.users.views import ChangePasswordView, ForgotPasswordView, LoginView, ResetPasswordView
from tests.users.test_password_reset import NEW, OLD, otp_from_outbox

User = get_user_model()


class Scenarios(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.u = User.objects.create_user("Mixed_Case", "mixed@x.com", OLD)

    def post(self, name, data):
        return self.client.post(reverse(f"auth:{name}"), data, format="json")

    def test_username_match_is_case_insensitive(self):
        self.post("password-forgot", {"identifier": " mixed_CASE "})
        self.assertEqual(len(mail.outbox), 1)

    def test_username_email_overlap_is_ambiguous(self):
        User.objects.create_user("other", "x@y.com", OLD)
        User.objects.create_user("x@y.com", "z@y.com", OLD)
        self.post("password-forgot", {"identifier": "x@y.com"})
        self.assertEqual(len(mail.outbox), 0)

    def test_throttle_scopes_configured(self):
        for view, scope in [
            (LoginView, "login"),
            (ForgotPasswordView, "password_forgot"),
            (ResetPasswordView, "password_reset"),
            (ChangePasswordView, "password_change"),
        ]:
            self.assertEqual(view.throttle_scope, scope)
            self.assertEqual(len(view.throttle_classes), 1)
        from django.conf import settings
        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        self.assertEqual(
            rates,
            {"login": "10/min", "password_forgot": "5/hour",
             "password_reset": "10/hour", "password_change": "10/hour"},
        )

    def test_email_failure_then_resend_works_immediately(self):
        with mock.patch("apps.users.services.send_mail", side_effect=OSError("down")):
            self.post("password-forgot", {"identifier": "Mixed_Case"})
        self.assertEqual(PasswordResetOTP.objects.count(), 0)
        self.post("password-forgot", {"identifier": "Mixed_Case"})
        self.assertEqual(len(mail.outbox), 1)

    def test_otp_and_password_never_logged(self):
        with self.assertLogs("apps.users", level="DEBUG") as cm, mock.patch(
            "apps.users.services.send_mail", side_effect=OSError("down")
        ):
            self.post("password-forgot", {"identifier": "Mixed_Case"})
        self.assertNotIn(self.u.password, "\n".join(cm.output))

    def test_inactive_user_cannot_reset_even_with_valid_otp(self):
        self.post("password-forgot", {"identifier": "Mixed_Case"})
        code = otp_from_outbox()
        User.objects.filter(pk=self.u.pk).update(is_active=False)
        r = self.post("password-reset", {"identifier": "Mixed_Case", "otp": code, "new_password": NEW})
        self.assertEqual(r.status_code, 400)
        self.u.refresh_from_db()
        self.assertTrue(self.u.check_password(OLD))

    def test_change_password_weak_and_inactive(self):
        self.client.force_authenticate(self.u)
        r = self.post("password-change", {"old_password": OLD, "new_password": "12345678"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("new_password", r.json())
        self.u.refresh_from_db()
        self.assertTrue(self.u.check_password(OLD))

    def test_second_live_otp_rejected_by_db(self):
        now = timezone.now()
        PasswordResetOTP.objects.create(user=self.u, code_hash="a", expires_at=now)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PasswordResetOTP.objects.create(user=self.u, code_hash="b", expires_at=now)

    def test_purge_keeps_recent_and_live_rows(self):
        now = timezone.now()
        old = PasswordResetOTP.objects.create(user=self.u, code_hash="a", expires_at=now, used_at=now)
        PasswordResetOTP.objects.filter(pk=old.pk).update(created_at=now - timedelta(days=2))
        recent = PasswordResetOTP.objects.create(user=self.u, code_hash="b", expires_at=now, used_at=now)
        live = PasswordResetOTP.objects.create(user=self.u, code_hash="c", expires_at=now + timedelta(minutes=5))
        for _ in range(2):  # rerun-safe
            call_command("purge_password_otps")
        self.assertEqual(
            set(PasswordResetOTP.objects.values_list("pk", flat=True)), {recent.pk, live.pk}
        )

    def test_services_do_not_touch_other_users(self):
        other = User.objects.create_user("bystander", "b@x.com", OLD)
        before = other.password
        self.post("password-forgot", {"identifier": "Mixed_Case"})
        self.post("password-reset", {"identifier": "Mixed_Case", "otp": otp_from_outbox(), "new_password": NEW})
        other.refresh_from_db()
        self.assertEqual(other.password, before)
        self.assertFalse(PasswordResetOTP.objects.filter(user=other).exists())


def run_threads(fn, n):
    barrier = threading.Barrier(n)
    results, errors = [], []

    def work(i):
        try:
            barrier.wait()
            results.append(fn(i))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            connections.close_all()

    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return results, errors


class PostgresConcurrencyTests(TransactionTestCase):
    """Real PostgreSQL row-lock behaviour; each thread has its own connection."""

    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.u = User.objects.create_user("racer", "racer@x.com", OLD)

    def test_concurrent_issue_respects_cooldown(self):
        _, errors = run_threads(lambda i: services.issue_otp("racer"), 8)
        self.assertEqual(errors, [])
        self.assertEqual(PasswordResetOTP.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_concurrent_reset_consumes_otp_once(self):
        services.issue_otp("racer")
        code = otp_from_outbox()

        def attempt(i):
            try:
                services.reset_password("racer", code, f"Winner-pass-{i}-xyz!")
                return True
            except services.InvalidOTP:
                return False

        results, errors = run_threads(attempt, 6)
        self.assertEqual(errors, [])
        self.assertEqual(results.count(True), 1)
        self.assertEqual(PasswordResetOTP.objects.filter(used_at__isnull=False).count(), 1)

    def test_concurrent_wrong_attempts_capped_at_five(self):
        services.issue_otp("racer")
        code = otp_from_outbox()
        wrong = "000000" if code != "000000" else "111111"

        def attempt(i):
            try:
                services.reset_password("racer", wrong, NEW)
            except services.InvalidOTP:
                pass

        _, errors = run_threads(attempt, 12)
        self.assertEqual(errors, [])
        self.assertEqual(PasswordResetOTP.objects.get().attempts, 5)
        with self.assertRaises(services.InvalidOTP):
            services.reset_password("racer", code, NEW)
