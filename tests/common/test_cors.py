from django.test import TestCase, override_settings


@override_settings(CORS_ALLOWED_ORIGINS=["https://app.example.com"], CORS_ALLOWED_ORIGIN_REGEXES=[])
class CorsTests(TestCase):
    def preflight(self, origin):
        return self.client.options(
            "/api/v1/auth/token/",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "authorization,content-type"},
        )

    def test_listed_origin_is_allowed(self):
        r = self.preflight("https://app.example.com")
        self.assertEqual(r["Access-Control-Allow-Origin"], "https://app.example.com")

    def test_unlisted_origin_is_not(self):
        self.assertNotIn("Access-Control-Allow-Origin", self.preflight("https://evil.example.com"))
