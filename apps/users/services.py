import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from .models import PasswordResetOTP

logger = logging.getLogger(__name__)
User = get_user_model()

OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN = timedelta(seconds=60)


class InvalidOTP(Exception):
    """Raised for any failed reset; deliberately gives no reason."""


def _hash(user, code):
    return salted_hmac("password-reset-otp", f"{user.pk}:{code}").hexdigest()


def resolve_user(identifier):
    """Active user matching username or email exactly once, else None."""
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    users = list(
        User.objects.filter(is_active=True).filter(
            Q(username__iexact=identifier) | Q(email__iexact=identifier)
        )[:2]
    )
    return users[0] if len(users) == 1 else None


def revoke_user_tokens(user):
    BlacklistedToken.objects.bulk_create(
        [
            BlacklistedToken(token=t)
            for t in OutstandingToken.objects.filter(
                user=user, blacklistedtoken__isnull=True
            )
        ],
        ignore_conflicts=True,
    )


def issue_otp(identifier):
    """Email a reset OTP if an eligible account exists. Never reveals whether."""
    user = resolve_user(identifier)
    if user is None or not user.email:
        return
    now = timezone.now()
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user.pk)
        if not locked.is_active or not locked.email:
            return
        if PasswordResetOTP.objects.filter(
            user=locked, created_at__gt=now - OTP_RESEND_COOLDOWN
        ).exists():
            return
        PasswordResetOTP.objects.filter(user=locked, used_at__isnull=True).update(
            used_at=now
        )
        code = f"{secrets.randbelow(10**6):06d}"
        ttl = settings.PASSWORD_RESET_OTP_TTL_MINUTES
        PasswordResetOTP.objects.create(
            user=locked,
            code_hash=_hash(locked, code),
            expires_at=now + timedelta(minutes=ttl),
        )
        try:
            send_mail(
                "Your password reset code",
                f"Your OTP is {code}. It expires in {ttl} minutes. "
                "If you did not request this, ignore this email.",
                None,
                [locked.email],
            )
        except Exception:
            logger.exception("Password reset email failed for user_id=%s", locked.pk)
            transaction.set_rollback(True)  # drops the new OTP, restores old state


def reset_password(identifier, code, new_password):
    user = resolve_user(identifier)
    if user is None:
        raise InvalidOTP
    now = timezone.now()
    ok = False
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user.pk)
        if not locked.is_active:
            raise InvalidOTP
        otp = (
            PasswordResetOTP.objects.select_for_update()
            .filter(user=locked, used_at__isnull=True, expires_at__gt=now)
            .order_by("-created_at")
            .first()
        )
        if otp is not None and otp.attempts < OTP_MAX_ATTEMPTS:
            if constant_time_compare(otp.code_hash, _hash(locked, code)):
                # ValidationError here rolls back, leaving the OTP usable.
                validate_password(new_password, locked)
                locked.set_password(new_password)
                locked.save(update_fields=["password"])
                otp.used_at = now
                otp.save(update_fields=["used_at"])
                revoke_user_tokens(locked)
                ok = True
            else:
                otp.attempts += 1
                otp.save(update_fields=["attempts"])
    if not ok:
        raise InvalidOTP


def change_password(user, old_password, new_password):
    """Returns False if old_password is wrong. Raises ValidationError if weak."""
    if not user.check_password(old_password):
        return False
    validate_password(new_password, user)
    with transaction.atomic():
        user.set_password(new_password)
        user.save(update_fields=["password"])
        revoke_user_tokens(user)
    return True
