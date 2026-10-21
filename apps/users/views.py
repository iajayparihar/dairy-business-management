from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from django.conf import settings
from django.core.cache import cache
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenBlacklistView, TokenObtainPairView

from . import services
from .serializers import (
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    ResetPasswordSerializer,
)


_Detail = inline_serializer("Detail", {"detail": serializers.CharField()})


def _doc(request):
    return extend_schema(
        request=request,
        responses={200: _Detail, 400: OpenApiResponse(description="Validation error")},
    )


class LoginSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Session id: copied into every access token and rotated refresh token,
        # so logout can kill the access tokens too.
        token["sid"] = token["jti"]
        return token


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


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
            cache.set(
                f"revoked_sid:{sid}",
                1,
                int(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds()),
            )
        return response
