"""Throttles must key on the real client address, not a header the client sets."""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient


class ThrottleSpoofingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_rotating_x_forwarded_for_does_not_reset_the_login_limit(self):
        url = "/api/v1/auth/token/"
        client = APIClient()
        codes = [
            client.post(
                url, {"username": "nobody", "password": "wrong"}, format="json",
                HTTP_X_FORWARDED_FOR=f"203.0.113.{i}",
            ).status_code
            for i in range(14)
        ]
        self.assertIn(429, codes, codes)
        self.assertEqual(codes.count(401), 10)  # the configured 10/min, then blocked

    def test_password_forgot_limit_survives_header_rotation(self):
        client = APIClient()
        url = "/api/v1/auth/password/forgot/"
        codes = [
            client.post(url, {"identifier": "nobody"}, format="json",
                        HTTP_X_FORWARDED_FOR=f"198.51.100.{i}").status_code
            for i in range(8)
        ]
        self.assertIn(429, codes, codes)
