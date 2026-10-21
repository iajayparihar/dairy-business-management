from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()


class AdminPasswordTests(TestCase):
    def test_admin_created_user_can_log_in(self):
        root = User.objects.create_superuser("root", "r@x.com", "pw-123456!x")
        self.client.force_login(root)
        r = self.client.post(
            "/admin/users/user/add/",
            {"username": "made", "password1": "Str0ng-pass-77!", "password2": "Str0ng-pass-77!"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(User.objects.get(username="made").check_password("Str0ng-pass-77!"))

    def test_admin_login_after_api_password_change(self):
        User.objects.create_superuser("su", "s@x.com", "old-pass-123!x")
        t = self.client.post("/api/v1/auth/token/", {"username": "su", "password": "old-pass-123!x"}, content_type="application/json").json()
        r = self.client.post(
            "/api/v1/auth/password/change/",
            {"old_password": "old-pass-123!x", "new_password": "New-pass-987!zz"},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + t["access"],
        )
        self.assertEqual(r.status_code, 200)
        self.client.logout()
        r = self.client.post("/admin/login/", {"username": "su", "password": "New-pass-987!zz", "next": "/admin/"})
        self.assertEqual(r.status_code, 302)
