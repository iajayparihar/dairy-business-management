from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient


class LogoutTests(TestCase):
    def test_logout_revokes_access_and_refresh(self):
        cache.clear()
        get_user_model().objects.create_user("lo", "l@x.com", "pw-123456!x")
        c = APIClient()
        t = c.post("/api/v1/auth/token/", {"username": "lo", "password": "pw-123456!x"}, format="json").json()
        rot = c.post("/api/v1/auth/token/refresh/", {"refresh": t["refresh"]}, format="json").json()
        me = lambda a: c.post("/api/v1/auth/password/change/", {}, format="json", HTTP_AUTHORIZATION=f"Bearer {a}").status_code
        self.assertEqual(me(rot["access"]), 400)  # authenticated, body invalid
        self.assertEqual(c.post("/api/v1/auth/logout/", {"refresh": rot["refresh"]}, format="json").status_code, 200)
        self.assertEqual(me(rot["access"]), 401)
        self.assertEqual(me(t["access"]), 401)
        self.assertEqual(c.post("/api/v1/auth/token/verify/", {"token": rot["refresh"]}, format="json").status_code, 401)
        self.assertEqual(c.post("/api/v1/auth/logout/", {"refresh": rot["access"]}, format="json").status_code, 401)
