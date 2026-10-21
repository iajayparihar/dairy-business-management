from django.conf import settings
from django.core.cache import cache
from django.utils.crypto import salted_hmac
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken


def password_stamp(user):
    """Changes whenever the password hash changes, so old tokens stop working."""
    return salted_hmac("jwt-password-stamp", user.password).hexdigest()[:16]


def revoke_session_access(sid):
    """Kill the access tokens of one login session (until they would expire anyway)."""
    cache.set(
        f"revoked_sid:{sid}",
        1,
        int(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds()),
    )


def check_session(token, user):
    """Reject tokens of a logged-out session or issued before the last password change."""
    if cache.get(f"revoked_sid:{token.get('sid')}"):
        raise InvalidToken("Token has been revoked")
    stamp = token.get("pwd")  # tokens issued before this claim existed carry none
    if stamp is not None and stamp != password_stamp(user):
        raise InvalidToken("Token has been revoked")


class RevocableJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        check_session(validated_token, user)
        return user


try:
    from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme

    class RevocableJWTScheme(SimpleJWTScheme):
        target_class = RevocableJWTAuthentication
        name = "jwtAuth"
except ImportError:  # docs-only dependency
    pass
