import logging
import secrets
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from .models import PasswordResetOTP
from .phone import normalize_phone

logger = logging.getLogger(__name__)
User = get_user_model()

OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN = timedelta(seconds=60)


def send_email(subject, body, to, on_fail=None):
    """Send off the request thread (after commit) so timing never leaks account state."""

    def run():
        try:
            send_mail(subject, body, None, [to])
        except Exception:
            logger.exception("Email %r failed", subject)
            if on_fail:
                on_fail()
        finally:
            if settings.EMAIL_ASYNC:
                connection.close()  # this thread's own connection

    if settings.EMAIL_ASYNC:
        transaction.on_commit(
            lambda: threading.Thread(target=run, daemon=True).start()
        )
    else:
        run()


def _notify_password_changed(user):
    if user.email:
        send_email(
            "Your password was changed",
            "The password of your account was just changed and you were signed out "
            "everywhere. If this was not you, reset your password now and contact "
            "your administrator.",
            user.email,
        )


class InvalidOTP(Exception):
    """Raised for any failed reset; deliberately gives no reason."""


def _hash(user, code):
    return salted_hmac("password-reset-otp", f"{user.pk}:{code}").hexdigest()


def resolve_user(identifier):
    """Active user matching username, email or phone exactly once, else None."""
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    match = Q(username__iexact=identifier) | Q(email__iexact=identifier)
    try:
        phone = normalize_phone(identifier)
    except ValueError:
        phone = ""
    if phone:
        match |= Q(phone_number=phone)
    users = list(User.objects.filter(is_active=True).filter(match)[:2])
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


def _new_otp(locked_user, now):
    """Retire the live code, create a fresh one. Caller holds the user row lock."""
    PasswordResetOTP.objects.filter(user=locked_user, used_at__isnull=True).update(
        used_at=now
    )
    code = f"{secrets.randbelow(10**6):06d}"
    otp = PasswordResetOTP.objects.create(
        user=locked_user,
        code_hash=_hash(locked_user, code),
        expires_at=now
        + timedelta(minutes=settings.PASSWORD_RESET_OTP_TTL_MINUTES),
    )
    return code, otp


def issue_code_for_user(user):
    """Reset code handed to a person by their owner (no email needed). -> (code, expires_at)."""
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user.pk)
        code, otp = _new_otp(locked, timezone.now())
    return code, otp.expires_at


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
        ttl = settings.PASSWORD_RESET_OTP_TTL_MINUTES
        code, otp = _new_otp(locked, now)
        otp_id = otp.pk
        send_email(
            "Your password reset code",
            f"Your OTP is {code}. It expires in {ttl} minutes. "
            "If you did not request this, ignore this email.",
            locked.email,
            # drop the unusable OTP so the user can ask again straight away
            on_fail=lambda: PasswordResetOTP.objects.filter(pk=otp_id).delete(),
        )


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
                _notify_password_changed(locked)
                ok = True
            else:
                otp.attempts += 1
                otp.save(update_fields=["attempts"])
    if not ok:
        raise InvalidOTP


def change_password(user, old_password, new_password):
    """Returns False if old_password is wrong. Raises ValidationError if weak."""
    with transaction.atomic():
        # Lock so two parallel changes cannot both pass the old-password check.
        locked = User.objects.select_for_update().get(pk=user.pk)
        if not locked.check_password(old_password):
            return False
        if old_password == new_password:
            raise DjangoValidationError("New password must be different from the old one.")
        validate_password(new_password, locked)
        locked.set_password(new_password)
        locked.save(update_fields=["password"])
        revoke_user_tokens(locked)
        _notify_password_changed(locked)
    return True
