from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.users.models import PasswordResetOTP

# Rows younger than this are kept: issue_otp's resend cooldown reads recent rows.
GRACE = timedelta(days=1)


class Command(BaseCommand):
    help = "Delete expired or used password reset OTPs older than one day. Safe to rerun."

    def handle(self, *args, **options):
        now = timezone.now()
        n, _ = PasswordResetOTP.objects.filter(created_at__lt=now - GRACE).filter(
            Q(expires_at__lt=now) | Q(used_at__isnull=False)
        ).delete()
        self.stdout.write(f"Deleted {n} OTP rows.")
