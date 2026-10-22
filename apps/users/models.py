from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower

from .phone import normalize_phone, validate_phone


class User(AbstractUser):
    # Stored as +91XXXXXXXXXX (see phone.py); unique when not blank.
    phone_number = models.CharField(
        max_length=15,
        blank=True,
        default="",
        validators=[validate_phone],
    )

    class Meta(AbstractUser.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["phone_number"],
                condition=~models.Q(phone_number=""),
                name="user_phone_unique_when_set",
            ),
            # resolve_user() matches usernames case-insensitively
            models.UniqueConstraint(Lower("username"), name="user_username_ci_unique"),
        ]

    def save(self, *args, **kwargs):
        fields = kwargs.get("update_fields")
        if fields is None or "phone_number" in fields:  # login's last_login save must never fail
            validate_phone(self.phone_number)
            self.phone_number = normalize_phone(self.phone_number)
        super().save(*args, **kwargs)

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
