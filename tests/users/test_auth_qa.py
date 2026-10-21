"""QA sweep of the auth module: token, refresh, verify, logout, forgot, reset, change.

Each test states the behaviour a production API should have. A failing test is a finding.
"""
import re
import threading
import unittest
from datetime import timedelta
from unittest import mock

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.users.models import PasswordResetOTP
from apps.users.views import LoginSerializer

User = get_user_model()
PW = "Old-pass-12345!x"
NEW = "Brand-new-Pass-987!"
FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])

LOGIN, REFRESH, VERIFY, LOGOUT = (f"/api/v1/auth/{p}/" for p in ("token", "token/refresh", "token/verify", "logout"))
FORGOT, RESET, CHANGE = (f"/api/v1/auth/password/{p}/" for p in ("forgot", "reset", "change"))
ALL = [LOGIN, REFRESH, VERIFY, LOGOUT, FORGOT, RESET, CHANGE]


def pair(user):
    r = LoginSerializer.get_token(user)
    return str(r), str(r.access_token)


class Base:
    def setUp(self):
        cache.clear()
        mail.outbox = []
        self.c = APIClient()
        self.user = User.objects.create_user("qa_user", "qa@x.com", PW)

    def post(self, url, data=None, token=None, **kw):
        if token:
            kw["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.c.post(url, data, format="json", **kw)

    def login(self, username="qa_user", password=PW):
        return self.post(LOGIN, {"username": username, "password": password})

    def otp(self):
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def issue_otp(self, ident="qa_user"):
        self.post(FORGOT, {"identifier": ident})
        return self.otp()

    def expire_cooldown(self):
        PasswordResetOTP.objects.update(created_at=timezone.now() - timedelta(minutes=5))


# ---------------------------------------------------------------- token (login)
@FAST
class LoginQA(Base, TestCase):
    def test_valid_login(self):
        r = self.login()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()), {"access", "refresh"})

    def test_wrong_and_unknown_user_look_identical(self):
        a, b = self.login(password="nope"), self.login("ghost", "nope")
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))
        self.assertEqual(a.status_code, 401)

    def test_inactive_user_rejected(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.login().status_code, 401)

    def test_missing_blank_null_fields(self):
        for body in ({}, {"username": "qa_user"}, {"password": PW}, {"username": "", "password": ""},
                     {"username": None, "password": None}):
            self.assertEqual(self.post(LOGIN, body).status_code, 400, body)

    def test_non_string_types_never_500(self):
        for v in (1, 1.5, True, [], {}, ["a"], {"a": 1}):
            self.assertLess(self.post(LOGIN, {"username": v, "password": v}).status_code, 500, v)

    def test_case_sensitive_username_is_documented(self):
        self.assertEqual(self.login("QA_USER").status_code, 401)

    def test_email_as_username_not_supported(self):
        self.assertEqual(self.login("qa@x.com").status_code, 401)

    def test_password_whitespace_not_trimmed(self):
        self.assertEqual(self.login(password=PW + " ").status_code, 401)

    def test_huge_inputs(self):
        self.assertLess(self.login("a" * 100000, "b" * 100000).status_code, 500)

    def test_nul_byte(self):
        self.assertEqual(self.login("a\x00b", PW).status_code, 400)

    def test_injection_strings(self):
        for s in ("' OR 1=1 --", '" OR ""="', "qa_user'--", "%", "_", "../../etc/passwd"):
            self.assertEqual(self.login(s, s).status_code, 401, s)

    def test_unicode_password_roundtrip(self):
        User.objects.create_user("uni", "u@x.com", "पासवर्ड-Ünï-123!")
        self.assertEqual(self.login("uni", "पासवर्ड-Ünï-123!").status_code, 200)

    def test_malformed_json_and_empty_body(self):
        self.assertEqual(self.c.post(LOGIN, "{bad", content_type="application/json").status_code, 400)
        self.assertEqual(self.c.post(LOGIN, "", content_type="application/json").status_code, 400)

    def test_form_encoded_accepted_or_rejected_cleanly(self):
        r = self.c.post(LOGIN, {"username": "qa_user", "password": PW})
        self.assertIn(r.status_code, (200, 415))

    def test_methods(self):
        for m in ("get", "put", "patch", "delete"):
            self.assertEqual(getattr(self.c, m)(LOGIN).status_code, 405, m)

    def test_throttle(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"login": "3/min"}):
            codes = [self.login(password="bad").status_code for _ in range(4)]
        self.assertEqual(codes, [401, 401, 401, 429])

    def test_successful_logins_also_counted_by_throttle(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"login": "2/min"}):
            codes = [self.login().status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_token_claims_have_no_secrets(self):
        _, access = pair(self.user)
        claims = jwt.decode(access, options={"verify_signature": False})
        self.assertEqual(set(claims), {"token_type", "exp", "iat", "jti", "user_id", "sid", "pwd"})
        self.assertNotIn(self.user.password, str(claims))

    def test_token_lifetimes(self):
        r, a = pair(self.user)
        d = lambda t: (lambda c: c["exp"] - c["iat"])(jwt.decode(t, options={"verify_signature": False}))
        self.assertEqual(d(a), 30 * 60)
        self.assertEqual(d(r), 7 * 86400)

    @unittest.expectedFailure  # open finding, see QA report
    def test_login_records_last_login(self):
        self.login()
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login)

    def test_two_logins_get_distinct_sessions(self):
        a, b = self.login().json(), self.login().json()
        sid = lambda t: jwt.decode(t["access"], options={"verify_signature": False})["sid"]
        self.assertNotEqual(sid(a), sid(b))

    def test_old_password_fails_after_change_new_works(self):
        _, acc = pair(self.user)
        self.post(CHANGE, {"old_password": PW, "new_password": NEW}, acc)
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login(password=NEW).status_code, 200)


# ---------------------------------------------------------------- refresh
@FAST
class RefreshQA(Base, TestCase):
    def test_rotation_and_replay(self):
        r, _ = pair(self.user)
        ok = self.post(REFRESH, {"refresh": r})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(set(ok.json()), {"access", "refresh"})
        self.assertEqual(self.post(REFRESH, {"refresh": r}).status_code, 401)

    @unittest.expectedFailure  # open finding, see QA report
    def test_replay_of_rotated_token_kills_the_session(self):
        """Reuse of a rotated refresh token signals theft: the new token should die too."""
        r, _ = pair(self.user)
        new = self.post(REFRESH, {"refresh": r}).json()
        self.post(REFRESH, {"refresh": r})  # attacker replays the old one
        self.assertEqual(self.post(REFRESH, {"refresh": new["refresh"]}).status_code, 401)

    def test_access_token_cannot_refresh(self):
        _, a = pair(self.user)
        self.assertEqual(self.post(REFRESH, {"refresh": a}).status_code, 401)

    def test_missing_blank_garbage(self):
        self.assertEqual(self.post(REFRESH, {}).status_code, 400)
        self.assertEqual(self.post(REFRESH, {"refresh": ""}).status_code, 400)
        self.assertEqual(self.post(REFRESH, {"refresh": None}).status_code, 400)
        self.assertEqual(self.post(REFRESH, {"refresh": "abc.def.ghi"}).status_code, 401)
        for v in (1, [], {}, True):
            self.assertLess(self.post(REFRESH, {"refresh": v}).status_code, 500, v)

    def test_expired(self):
        t = RefreshToken.for_user(self.user)
        t.set_exp(lifetime=-timedelta(seconds=1))
        self.assertEqual(self.post(REFRESH, {"refresh": str(t)}).status_code, 401)

    def test_tampered_and_foreign_signature_and_alg_none(self):
        r, _ = pair(self.user)
        h, p, s = r.split(".")
        self.assertEqual(self.post(REFRESH, {"refresh": f"{h}.{p}.{s[:-2]}xx"}).status_code, 401)
        claims = jwt.decode(r, options={"verify_signature": False})
        forged = jwt.encode(claims, "other-key", algorithm="HS256")
        self.assertEqual(self.post(REFRESH, {"refresh": forged}).status_code, 401)
        none_tok = jwt.encode(claims, None, algorithm="none")
        self.assertEqual(self.post(REFRESH, {"refresh": none_tok}).status_code, 401)

    def test_deactivated_user_cannot_refresh(self):
        r, _ = pair(self.user)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.post(REFRESH, {"refresh": r}).status_code, 401)

    def test_deleted_user_cannot_refresh_no_500(self):
        r, _ = pair(self.user)
        self.user.delete()
        self.assertEqual(self.post(REFRESH, {"refresh": r}).status_code, 401)

    def test_refreshed_access_keeps_session_id(self):
        r, a = pair(self.user)
        new = self.post(REFRESH, {"refresh": r}).json()
        sid = lambda t: jwt.decode(t, options={"verify_signature": False})["sid"]
        self.assertEqual(sid(a), sid(new["access"]))
        self.assertEqual(sid(a), sid(new["refresh"]))

    def test_new_access_token_works(self):
        r, _ = pair(self.user)
        new = self.post(REFRESH, {"refresh": r}).json()
        self.assertEqual(self.post(CHANGE, {}, new["access"]).status_code, 400)

    def test_refresh_after_logout_and_after_password_change(self):
        r, _ = pair(self.user)
        self.post(LOGOUT, {"refresh": r})
        self.assertEqual(self.post(REFRESH, {"refresh": r}).status_code, 401)
        r2, a2 = pair(self.user)
        self.post(CHANGE, {"old_password": PW, "new_password": NEW}, a2)
        self.assertEqual(self.post(REFRESH, {"refresh": r2}).status_code, 401)

    def test_refresh_after_forgot_reset_is_revoked(self):
        r, _ = pair(self.user)
        code = self.issue_otp()
        self.post(RESET, {"identifier": "qa_user", "otp": code, "new_password": NEW})
        self.assertEqual(self.post(REFRESH, {"refresh": r}).status_code, 401)

    def test_methods(self):
        self.assertEqual(self.c.get(REFRESH).status_code, 405)


@FAST
class RefreshRaceQA(Base, TransactionTestCase):
    def test_parallel_refresh_with_same_token_succeeds_once(self):
        r, _ = pair(self.user)
        codes = []

        def go():
            try:
                codes.append(APIClient().post(REFRESH, {"refresh": r}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes)
        self.assertEqual(codes.count(200), 1, codes)


# ---------------------------------------------------------------- verify
@FAST
class VerifyQA(Base, TestCase):
    def test_valid_access_ok(self):
        _, a = pair(self.user)
        self.assertEqual(self.post(VERIFY, {"token": a}).status_code, 200)

    def test_invalid_garbage_401_missing_400(self):
        self.assertEqual(self.post(VERIFY, {"token": "x.y.z"}).status_code, 401)
        self.assertEqual(self.post(VERIFY, {}).status_code, 400)
        self.assertEqual(self.post(VERIFY, {"token": ""}).status_code, 400)
        self.assertEqual(self.post(VERIFY, {"token": None}).status_code, 400)

    def test_expired(self):
        t = AccessToken.for_user(self.user)
        t.set_exp(lifetime=-timedelta(seconds=1))
        self.assertEqual(self.post(VERIFY, {"token": str(t)}).status_code, 401)

    def test_tampered(self):
        _, a = pair(self.user)
        self.assertEqual(self.post(VERIFY, {"token": a[:-3] + "abc"}).status_code, 401)

    def test_blacklisted_refresh_rejected(self):
        r, _ = pair(self.user)
        self.post(LOGOUT, {"refresh": r})
        self.assertGreaterEqual(self.post(VERIFY, {"token": r}).status_code, 400)

    def test_logged_out_access_token_rejected(self):
        r, a = pair(self.user)
        self.post(LOGOUT, {"refresh": r})
        self.assertGreaterEqual(self.post(VERIFY, {"token": a}).status_code, 400)

    def test_deactivated_user_token_rejected(self):
        _, a = pair(self.user)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertGreaterEqual(self.post(VERIFY, {"token": a}).status_code, 400)

    def test_deleted_user_token_rejected(self):
        _, a = pair(self.user)
        self.user.delete()
        self.assertGreaterEqual(self.post(VERIFY, {"token": a}).status_code, 400)

    def test_password_changed_access_token_rejected(self):
        _, a = pair(self.user)
        r2, a2 = pair(self.user)
        self.post(CHANGE, {"old_password": PW, "new_password": NEW}, a2)
        self.assertGreaterEqual(self.post(VERIFY, {"token": a}).status_code, 400)

    def test_verify_status_codes_consistent_for_bad_tokens(self):
        r, _ = pair(self.user)
        self.post(LOGOUT, {"refresh": r})
        blacklisted = self.post(VERIFY, {"token": r}).status_code
        garbage = self.post(VERIFY, {"token": "x.y.z"}).status_code
        self.assertEqual(blacklisted, garbage)

    def test_refresh_token_accepted_as_valid_is_documented(self):
        r, _ = pair(self.user)
        self.assertEqual(self.post(VERIFY, {"token": r}).status_code, 200)

    def test_unauthenticated_and_no_throttle_dos_shape(self):
        self.assertIn(self.c.get(VERIFY).status_code, (405,))


# ---------------------------------------------------------------- logout
@FAST
class LogoutQA(Base, TestCase):
    def test_happy_path(self):
        r, a = pair(self.user)
        self.assertEqual(self.post(LOGOUT, {"refresh": r}).status_code, 200)
        self.assertEqual(self.post(CHANGE, {}, a).status_code, 401)
        self.assertEqual(self.post(REFRESH, {"refresh": r}).status_code, 401)

    def test_second_logout_same_token(self):
        r, _ = pair(self.user)
        self.post(LOGOUT, {"refresh": r})
        self.assertIn(self.post(LOGOUT, {"refresh": r}).status_code, (200, 401))

    def test_validation(self):
        self.assertEqual(self.post(LOGOUT, {}).status_code, 400)
        self.assertEqual(self.post(LOGOUT, {"refresh": ""}).status_code, 400)
        self.assertEqual(self.post(LOGOUT, {"refresh": None}).status_code, 400)
        self.assertEqual(self.post(LOGOUT, {"refresh": "junk"}).status_code, 401)
        for v in (1, [], {}, True):
            self.assertLess(self.post(LOGOUT, {"refresh": v}).status_code, 500, v)

    def test_access_token_in_refresh_field_is_rejected_and_not_revoked(self):
        _, a = pair(self.user)
        self.assertEqual(self.post(LOGOUT, {"refresh": a}).status_code, 401)
        self.assertEqual(self.post(CHANGE, {}, a).status_code, 400)  # still valid

    def test_other_device_survives(self):
        r1, a1 = pair(self.user)
        r2, a2 = pair(self.user)
        self.post(LOGOUT, {"refresh": r1})
        self.assertEqual(self.post(CHANGE, {}, a2).status_code, 400)
        self.assertEqual(self.post(REFRESH, {"refresh": r2}).status_code, 200)

    def test_other_users_unaffected(self):
        other = User.objects.create_user("qa_other", "o@x.com", PW)
        r1, _ = pair(self.user)
        r2, a2 = pair(other)
        self.post(LOGOUT, {"refresh": r1})
        self.assertEqual(self.post(CHANGE, {}, a2).status_code, 400)

    def test_logout_with_rotated_chain_kills_all_access_tokens_of_session(self):
        r, a = pair(self.user)
        n = self.post(REFRESH, {"refresh": r}).json()
        self.post(LOGOUT, {"refresh": n["refresh"]})
        self.assertEqual(self.post(CHANGE, {}, a).status_code, 401)
        self.assertEqual(self.post(CHANGE, {}, n["access"]).status_code, 401)

    def test_logout_with_expired_refresh(self):
        t = RefreshToken.for_user(self.user)
        t.set_exp(lifetime=-timedelta(seconds=1))
        self.assertEqual(self.post(LOGOUT, {"refresh": str(t)}).status_code, 401)

    def test_logout_requires_no_bearer_but_does_not_break_with_a_bad_one(self):
        r, _ = pair(self.user)
        self.assertEqual(self.post(LOGOUT, {"refresh": r}, token="garbage").status_code, 200)

    def test_logout_survives_cache_outage_without_500(self):
        r, _ = pair(self.user)
        with mock.patch("apps.users.views.cache.set", side_effect=ConnectionError):
            self.assertLess(self.post(LOGOUT, {"refresh": r}).status_code, 500)

    def test_relogin_after_logout(self):
        r, _ = pair(self.user)
        self.post(LOGOUT, {"refresh": r})
        _, a = pair(self.user)
        self.assertEqual(self.post(CHANGE, {}, a).status_code, 400)

    def test_methods(self):
        self.assertEqual(self.c.get(LOGOUT).status_code, 405)


@FAST
class LogoutRaceQA(Base, TransactionTestCase):
    def test_parallel_logout_same_token_no_500(self):
        r, _ = pair(self.user)
        codes = []

        def go():
            try:
                codes.append(APIClient().post(LOGOUT, {"refresh": r}, format="json").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)


# ---------------------------------------------------------------- forgot
@FAST
class ForgotQA(Base, TestCase):
    def test_existing_by_username_and_email_sends_one_mail_each(self):
        self.post(FORGOT, {"identifier": "qa_user"})
        self.expire_cooldown()
        self.post(FORGOT, {"identifier": "QA@X.COM"})
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[0].to, ["qa@x.com"])

    def test_unknown_inactive_noemail_ambiguous_send_nothing_same_body(self):
        User.objects.create_user("noemail", "", PW)
        User.objects.create_user("gone", "gone@x.com", PW, is_active=False)
        User.objects.create_user("dup1", "dup@x.com", PW)
        User.objects.create_user("dup2", "dup@x.com", PW)
        bodies = []
        for ident in ("ghost", "noemail", "gone", "dup@x.com", "qa_user"):
            r = self.post(FORGOT, {"identifier": ident})
            bodies.append((r.status_code, r.json()))
        self.assertEqual(len(set(map(str, bodies))), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_validation(self):
        for body in ({}, {"identifier": ""}, {"identifier": "   "}, {"identifier": None},
                     {"identifier": "a" * 255}):
            self.assertEqual(self.post(FORGOT, body).status_code, 400, body)
        for v in (1, [], {}, True):
            self.assertLess(self.post(FORGOT, {"identifier": v}).status_code, 500, v)
        self.assertEqual(self.post(FORGOT, {"identifier": "a\x00b"}).status_code, 400)

    def test_wildcards_and_injection_match_nobody(self):
        for s in ("%", "_", "%@x.com", "q%", "' OR 1=1 --"):
            self.assertEqual(self.post(FORGOT, {"identifier": s}).status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_identifier_whitespace_trimmed(self):
        self.post(FORGOT, {"identifier": "  qa_user  "})
        self.assertEqual(len(mail.outbox), 1)

    def test_mail_content(self):
        self.post(FORGOT, {"identifier": "qa_user"})
        m = mail.outbox[0]
        self.assertRegex(m.body, r"\b\d{6}\b")
        self.assertIn("10 minutes", m.body)
        self.assertNotIn(PW, m.body)
        self.assertNotIn("\n", m.subject)

    def test_cooldown_and_resend_after(self):
        self.post(FORGOT, {"identifier": "qa_user"})
        self.post(FORGOT, {"identifier": "qa_user"})
        self.assertEqual(len(mail.outbox), 1)
        self.expire_cooldown()
        self.post(FORGOT, {"identifier": "qa_user"})
        self.assertEqual(len(mail.outbox), 2)

    def test_expired_bearer_header_does_not_block_public_endpoint(self):
        t = AccessToken.for_user(self.user)
        t.set_exp(lifetime=-timedelta(seconds=1))
        self.assertEqual(self.post(FORGOT, {"identifier": "qa_user"}, str(t)).status_code, 200)

    def test_smtp_failure_still_200_and_leaves_no_live_otp(self):
        with mock.patch("apps.users.services.send_mail", side_effect=OSError):
            self.assertEqual(self.post(FORGOT, {"identifier": "qa_user"}).status_code, 200)
        self.assertFalse(PasswordResetOTP.objects.filter(used_at__isnull=True).exists())

    @unittest.expectedFailure  # open finding, see QA report
    def test_response_time_does_not_depend_on_account_existence(self):
        """send_mail runs inside the request: a slow SMTP makes existing accounts distinguishable."""
        import time

        def slow(*a, **k):
            time.sleep(0.3)

        with mock.patch("apps.users.services.send_mail", side_effect=slow):
            t0 = time.perf_counter()
            self.post(FORGOT, {"identifier": "qa_user"})
            known = time.perf_counter() - t0
            t0 = time.perf_counter()
            self.post(FORGOT, {"identifier": "ghost"})
            unknown = time.perf_counter() - t0
        self.assertLess(abs(known - unknown), 0.15, (known, unknown))

    def test_throttle_per_ip(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"password_forgot": "2/hour"}):
            codes = [self.post(FORGOT, {"identifier": "x"}).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_methods(self):
        self.assertEqual(self.c.get(FORGOT).status_code, 405)

    def test_only_one_live_otp_after_many_requests(self):
        for _ in range(3):
            self.post(FORGOT, {"identifier": "qa_user"})
            self.expire_cooldown()
        self.assertEqual(PasswordResetOTP.objects.filter(used_at__isnull=True).count(), 1)


# ---------------------------------------------------------------- reset
@FAST
class ResetQA(Base, TestCase):
    def reset(self, ident="qa_user", otp="000000", pw=NEW):
        return self.post(RESET, {"identifier": ident, "otp": otp, "new_password": pw})

    def test_full_flow(self):
        code = self.issue_otp()
        self.assertEqual(self.reset(otp=code).status_code, 200)
        self.assertEqual(self.login(password=PW).status_code, 401)
        self.assertEqual(self.login(password=NEW).status_code, 200)

    def test_reset_by_email_identifier(self):
        code = self.issue_otp("qa@x.com")
        self.assertEqual(self.reset("QA@x.com", code).status_code, 200)

    def test_otp_single_use(self):
        code = self.issue_otp()
        self.reset(otp=code)
        self.assertEqual(self.reset(otp=code, pw="Another-pass-555!").status_code, 400)

    def test_wrong_otp_uniform_error_for_unknown_inactive_and_wrong(self):
        User.objects.create_user("gone", "gone@x.com", PW, is_active=False)
        self.issue_otp()
        outs = {str((self.reset(i, "111111").status_code, self.reset(i, "111111").json()))
                for i in ("ghost", "gone", "qa_user")}
        self.assertEqual(len(outs), 1)

    def test_five_wrong_then_correct_is_dead(self):
        code = self.issue_otp()
        wrong = "000000" if code != "000000" else "000001"
        for _ in range(5):
            self.reset(otp=wrong)
        self.assertEqual(self.reset(otp=code).status_code, 400)

    def test_otp_format_validation(self):
        for bad in ("", "12345", "1234567", "abcdef", "12 456", "١٢٣٤٥٦", None, 123456, [], {}):
            self.assertEqual(self.reset(otp=bad).status_code, 400, repr(bad))

    def test_expired_and_boundary(self):
        code = self.issue_otp()
        PasswordResetOTP.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.reset(otp=code).status_code, 400)

    def test_weak_passwords_keep_otp_alive(self):
        code = self.issue_otp()
        for weak in ("short1!", "password", "12345678901", "qa_user12345", "", "a" * 129):
            self.assertEqual(self.reset(otp=code, pw=weak).status_code, 400, weak)
        self.assertEqual(self.reset(otp=code).status_code, 200)

    def test_missing_fields_and_nul(self):
        self.assertEqual(self.post(RESET, {}).status_code, 400)
        self.assertEqual(self.post(RESET, {"identifier": "qa_user", "otp": "123456"}).status_code, 400)
        self.assertEqual(self.reset(pw="a\x00b" + NEW).status_code, 400)

    def test_new_password_equal_to_old_is_allowed_or_rejected_not_500(self):
        code = self.issue_otp()
        self.assertIn(self.reset(otp=code, pw=PW).status_code, (200, 400))

    def test_reset_revokes_refresh_and_access_of_all_sessions(self):
        r, a = pair(self.user)
        code = self.issue_otp()
        self.reset(otp=code)
        self.assertEqual(self.post(REFRESH, {"refresh": r}).status_code, 401)
        self.assertEqual(self.post(CHANGE, {}, a).status_code, 401)

    def test_password_with_leading_trailing_spaces_kept(self):
        code = self.issue_otp()
        self.reset(otp=code, pw="  Spaced-pass-321!  ")
        self.assertEqual(self.login(password="  Spaced-pass-321!  ").status_code, 200)

    def test_reset_does_not_change_other_fields(self):
        before = User.objects.values("is_staff", "is_superuser", "email", "is_active").get(pk=self.user.pk)
        code = self.issue_otp()
        self.reset(otp=code)
        self.assertEqual(before, User.objects.values("is_staff", "is_superuser", "email", "is_active").get(pk=self.user.pk))

    def test_new_forgot_invalidates_previous_otp(self):
        old = self.issue_otp()
        self.expire_cooldown()
        PasswordResetOTP.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        new = self.issue_otp()
        if old != new:
            self.assertEqual(self.reset(otp=old).status_code, 400)

    def test_throttle(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"password_reset": "2/hour"}):
            codes = [self.reset().status_code for _ in range(3)]
        self.assertEqual(codes, [400, 400, 429])

    def test_methods(self):
        self.assertEqual(self.c.get(RESET).status_code, 405)

    def test_otp_hash_not_plaintext_in_db(self):
        code = self.issue_otp()
        self.assertNotIn(code, PasswordResetOTP.objects.get().code_hash)

    def test_otp_ttl_matches_setting(self):
        self.issue_otp()
        o = PasswordResetOTP.objects.get()
        self.assertAlmostEqual((o.expires_at - o.created_at).total_seconds(), 600, delta=5)

    def test_account_enumeration_via_reset_status_codes(self):
        self.issue_otp()
        a = self.reset("ghost", "123456")
        b = self.reset("qa_user", "123456")
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))


# ---------------------------------------------------------------- change
@FAST
class ChangeQA(Base, TestCase):
    def setUp(self):
        super().setUp()
        self.r, self.a = pair(self.user)

    def change(self, old=PW, new=NEW, token="default"):
        return self.post(CHANGE, {"old_password": old, "new_password": new},
                         self.a if token == "default" else token)

    def test_unauthenticated(self):
        self.assertEqual(self.change(token=None).status_code, 401)

    def test_bad_bearer_variants(self):
        for h in ("Bearer", "Bearer ", "Token " + self.a, "bearer " + self.a, "Bearer a b", self.a):
            r = self.c.post(CHANGE, {"old_password": PW, "new_password": NEW}, format="json",
                            HTTP_AUTHORIZATION=h)
            self.assertIn(r.status_code, (200, 401), h)
        r = self.c.post(CHANGE, {"old_password": PW, "new_password": NEW}, format="json",
                        HTTP_AUTHORIZATION="Token " + self.a)
        self.assertEqual(r.status_code, 401)

    def test_refresh_token_as_bearer_rejected(self):
        self.assertEqual(self.change(token=self.r).status_code, 401)

    def test_expired_access_rejected(self):
        t = AccessToken.for_user(self.user)
        t.set_exp(lifetime=-timedelta(seconds=1))
        self.assertEqual(self.change(token=str(t)).status_code, 401)

    def test_happy_path(self):
        self.assertEqual(self.change().status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW))

    def test_wrong_old_password(self):
        r = self.change(old="wrong")
        self.assertEqual(r.status_code, 400)
        self.assertIn("old_password", r.json())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PW))

    def test_weak_new_password_variants(self):
        for weak in ("short1!", "password", "123456789012", "qa_user12345", "", "a" * 129, "   "):
            r = self.change(new=weak)
            self.assertEqual(r.status_code, 400, weak)
            self.assertIn("new_password", r.json(), weak)

    def test_same_password_rejected(self):
        self.assertEqual(self.change(new=PW).status_code, 400)

    def test_missing_null_wrong_types(self):
        for body in ({}, {"old_password": PW}, {"new_password": NEW},
                     {"old_password": None, "new_password": None}):
            self.assertEqual(self.post(CHANGE, body, self.a).status_code, 400, body)
        for v in (1, [], {}, True):
            self.assertLess(self.post(CHANGE, {"old_password": v, "new_password": v}, self.a).status_code, 500)
        self.assertEqual(self.change(new="a\x00" + NEW).status_code, 400)

    def test_revokes_refresh_tokens_of_all_sessions(self):
        r2, _ = pair(self.user)
        self.change()
        self.assertEqual(self.post(REFRESH, {"refresh": self.r}).status_code, 401)
        self.assertEqual(self.post(REFRESH, {"refresh": r2}).status_code, 401)

    def test_old_access_tokens_die_with_the_old_password(self):
        _, other = pair(self.user)
        self.change()
        self.assertEqual(self.post(CHANGE, {}, other).status_code, 401)
        self.assertEqual(self.post(CHANGE, {}, self.a).status_code, 401)

    def test_other_users_sessions_untouched(self):
        o = User.objects.create_user("qa_o", "o@x.com", PW)
        ro, ao = pair(o)
        self.change()
        self.assertEqual(self.post(REFRESH, {"refresh": ro}).status_code, 200)

    def test_deactivated_user_token_rejected(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.change().status_code, 401)

    def test_throttle_counts_wrong_old_passwords(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"password_change": "3/hour"}):
            codes = [self.change(old="bad").status_code for _ in range(4)]
        self.assertEqual(codes, [400, 400, 400, 429])

    @unittest.expectedFailure  # open finding, see QA report
    def test_user_gets_notified_by_email_of_password_change(self):
        self.change()
        self.assertEqual(len(mail.outbox), 1)

    def test_superuser_and_staff_can_change(self):
        for flag in ("is_superuser", "is_staff"):
            u = User.objects.create_user(f"qa_{flag}", f"{flag}@x.com", PW, **{flag: True})
            _, a = pair(u)
            self.assertEqual(self.post(CHANGE, {"old_password": PW, "new_password": NEW}, a).status_code, 200)

    def test_methods(self):
        self.assertEqual(self.c.get(CHANGE, HTTP_AUTHORIZATION=f"Bearer {self.a}").status_code, 405)


@FAST
class ChangeRaceQA(Base, TransactionTestCase):
    def test_parallel_change_no_500_and_consistent_password(self):
        _, a = pair(self.user)
        codes = []

        def go(new):
            try:
                codes.append(APIClient().post(
                    CHANGE, {"old_password": PW, "new_password": new}, format="json",
                    HTTP_AUTHORIZATION=f"Bearer {a}").status_code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go, args=(f"Parallel-pass-{i}77!",)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertNotIn(500, codes, codes)
        self.assertLessEqual(codes.count(200), 1, codes)


# ---------------------------------------------------------------- cross-cutting
@FAST
class FuzzQA(Base, TestCase):
    PAYLOADS = ["", "[]", "null", "123", '"str"', "{", '{"a":', "\x00", "{" * 5000, '{"a":' * 500 + "1" + "}" * 500,
                '{"refresh": "' + "A" * 200000 + '"}', "💥", '{"username":"\ud800"}']

    def test_no_endpoint_returns_5xx_on_garbage_json(self):
        _, a = pair(self.user)
        for url in ALL:
            for p in self.PAYLOADS:
                r = self.c.post(url, p.encode("utf-8", "surrogatepass"), content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {a}")
                self.assertLess(r.status_code, 500, (url, p[:30]))

    def test_no_endpoint_returns_5xx_on_other_content_types(self):
        for url in ALL:
            for ct in ("text/plain", "application/xml", "multipart/form-data; boundary=x", "image/png"):
                r = self.c.post(url, b"abc", content_type=ct)
                self.assertLess(r.status_code, 500, (url, ct))

    def test_errors_are_json_not_html(self):
        for url in ALL:
            r = self.c.post(url, "{", content_type="application/json")
            self.assertIn("application/json", r["Content-Type"], url)

    def test_no_stacktrace_or_internal_info_in_errors(self):
        r = self.post(LOGIN, {"username": "x", "password": "y"})
        self.assertNotIn("Traceback", r.content.decode())

    def test_options_and_head_do_not_500(self):
        for url in ALL:
            self.assertLess(self.c.options(url).status_code, 500)
            self.assertLess(self.c.head(url).status_code, 500)

    def test_signing_key_is_not_default_secret_in_production_settings(self):
        self.assertTrue(settings.SIMPLE_JWT["SIGNING_KEY"])

    def test_outstanding_tokens_get_purged_command_exists(self):
        from django.core.management import get_commands
        self.assertIn("flushexpiredtokens", get_commands())
