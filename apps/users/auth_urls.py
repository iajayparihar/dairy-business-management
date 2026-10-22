
from django.urls import path

from . import views

app_name = "auth"

urlpatterns = [
    path("token/", views.LoginView.as_view(), name="token-obtain"),
    path("token/refresh/", views.RefreshView.as_view(), name="token-refresh"),
    path("token/verify/", views.VerifyView.as_view(), name="token-verify"),
    path("me/", views.MeView.as_view(), name="me"),
    path("logout/", views.LogoutView.as_view(), name="token-blacklist"),
    path("password/forgot/", views.ForgotPasswordView.as_view(), name="password-forgot"),
    path("password/reset/", views.ResetPasswordView.as_view(), name="password-reset"),
    path("password/change/", views.ChangePasswordView.as_view(), name="password-change"),
]
