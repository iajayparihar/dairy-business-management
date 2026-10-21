from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer,
    TokenRefreshSerializer,
)
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken, UntypedToken
from rest_framework_simplejwt.views import (
    TokenBlacklistView,
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

from .authentication import check_session, password_stamp, revoke_session_access

import logging
from datetime import timedelta

from . import services
from .serializers import (
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    ResetPasswordSerializer,
)


logger = logging.getLogger(__name__)
User = get_user_model()

_Detail = inline_serializer("Detail", {"detail": serializers.CharField()})


def _doc(request):
    return extend_schema(
        request=request,
        responses={200: _Detail, 400: OpenApiResponse(description="Validation error")},
    )


class LoginSerializer(TokenObtainPairSerializer):
    # Passwords are stored exactly as typed (change / reset keep spaces), so login
    # must not trim them either.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password"] = serializers.CharField(
            write_only=True, trim_whitespace=False, style={"input_type": "password"}
        )

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Session id: copied into every access token and rotated refresh token,
        # so logout can kill the access tokens too.
        token["sid"] = token["jti"]
        token["pwd"] = password_stamp(user)  # dies with the password
        return token


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


# A rotated token used again within this window is a double click / second tab, not theft.
REPLAY_GRACE = timedelta(seconds=10)


class RefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        try:  # decoded without the blacklist check, to see who is knocking
            old = UntypedToken(attrs["refresh"])
            if old["token_type"] != "refresh":
                old = None
        except Exception:  # invalid token: super() raises the proper error
            old = None
        try:
            # Serialize concurrent uses of one refresh token: simplejwt checks the
            # blacklist and then blacklists, so parallel requests all used to pass.
            with transaction.atomic():
                if old:
                    OutstandingToken.objects.select_for_update().filter(
                        jti=old["jti"]
                    ).first()
                try:
                    return super().validate(attrs)
                except User.DoesNotExist:  # user deleted after the token was issued
                    raise InvalidToken("User no longer exists")
        except (InvalidToken, TokenError):
            if old and BlacklistedToken.objects.filter(
                token__jti=old["jti"],
                blacklisted_at__lt=timezone.now() - REPLAY_GRACE,
            ).exists():
                # An already-used refresh token came back: assume it was stolen and
                # end this user's sessions. ponytail: all devices, not just this one
                # (OutstandingToken has no session id); add one if that hurts.
                user = User.objects.filter(pk=old["user_id"]).first()
                if user:
                    services.revoke_user_tokens(user)
                    revoke_session_access(old.get("sid"))
            raise


class RefreshView(TokenRefreshView):
    serializer_class = RefreshSerializer


class VerifyView(TokenVerifyView):
    """Also rejects tokens of deleted / inactive users and revoked sessions."""

    def post(self, request, *args, **kwargs):
        try:
            response = super().post(request, *args, **kwargs)
        except ValidationError as e:
            if "non_field_errors" in e.detail:  # blacklisted: same 401 as any bad token
                raise InvalidToken("Token is blacklisted")
            raise
        token = UntypedToken(request.data["token"])
        user = User.objects.filter(pk=token["user_id"], is_active=True).first()
        if user is None:
            raise InvalidToken("User not found or inactive")
        check_session(token, user)
        return response


class _PasswordView(APIView):
    throttle_classes = [ScopedRateThrottle]


@_doc(ForgotPasswordSerializer)
class ForgotPasswordView(_PasswordView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "password_forgot"

    def post(self, request):
        s = ForgotPasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        services.issue_otp(s.validated_data["identifier"])
        return Response({"detail": "If the account exists, an OTP has been sent."})


@_doc(ResetPasswordSerializer)
class ResetPasswordView(_PasswordView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "password_reset"

    def post(self, request):
        s = ResetPasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        try:
            services.reset_password(d["identifier"], d["otp"], d["new_password"])
        except services.InvalidOTP:
            return Response(
                {"detail": "Invalid or expired OTP."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except DjangoValidationError as e:
            return Response(
                {"new_password": e.messages}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response({"detail": "Password reset successful."})


@_doc(ChangePasswordSerializer)
class ChangePasswordView(_PasswordView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "password_change"

    def post(self, request):
        s = ChangePasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        try:
            changed = services.change_password(
                request.user, d["old_password"], d["new_password"]
            )
        except DjangoValidationError as e:
            return Response(
                {"new_password": e.messages}, status=status.HTTP_400_BAD_REQUEST
            )
        if not changed:
            return Response(
                {"old_password": ["Incorrect password."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"detail": "Password changed. Please log in again."})


class LogoutView(TokenBlacklistView):
    """Blacklist the refresh token and revoke the access tokens of its session."""

    def post(self, request, *args, **kwargs):
        try:
            sid = RefreshToken(request.data.get("refresh")).get("sid")
        except Exception:  # invalid token: the parent view returns the proper error
            sid = None
        response = super().post(request, *args, **kwargs)
        if sid:
            # ponytail: cache-backed; needs the shared (Redis) cache in production
            try:
                revoke_session_access(sid)
            except Exception:  # refresh token is already blacklisted: do not fail logout
                logger.exception("Could not revoke access tokens of the session")
        return response
