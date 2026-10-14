from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    phone_number = models.CharField(
        max_length=15,
        blank=True,
        default="",
    )

    def __str__(self):
        return self.username


class PasswordResetOTP(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="password_reset_otps",
    )
    code_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["user", "used_at", "expires_at"],
                name="pwotp_user_active_idx",
            ),
            models.Index(
                fields=["user", "-created_at"],
                name="pwotp_user_created_idx",
            ),
        ]
        constraints = [
            # DB-level backstop: at most one live OTP per user, even if locking is bypassed.
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(used_at__isnull=True),
                name="pwotp_one_live_per_user",
            ),
        ]
