from django.core.cache import cache
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken


class RevocableJWTAuthentication(JWTAuthentication):
    """JWT auth that rejects access tokens whose session was logged out."""

    def get_validated_token(self, raw_token):
        token = super().get_validated_token(raw_token)
        if cache.get(f"revoked_sid:{token.get('sid')}"):
            raise InvalidToken("Token has been revoked")
        return token


try:
    from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme

    class RevocableJWTScheme(SimpleJWTScheme):
        target_class = RevocableJWTAuthentication
        name = "jwtAuth"
except ImportError:  # docs-only dependency
    pass
