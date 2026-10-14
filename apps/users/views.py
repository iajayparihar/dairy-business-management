from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from . import services
from .serializers import (
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    ResetPasswordSerializer,
)


class LoginView(TokenObtainPairView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class _PasswordView(APIView):
    throttle_classes = [ScopedRateThrottle]


class ForgotPasswordView(_PasswordView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "password_forgot"

    def post(self, request):
        s = ForgotPasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        services.issue_otp(s.validated_data["identifier"])
        return Response({"detail": "If the account exists, an OTP has been sent."})


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
