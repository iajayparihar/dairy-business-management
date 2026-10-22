from rest_framework import serializers


class ForgotPasswordSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=254)


class ResetPasswordSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=254)
    otp = serializers.RegexField(r"^\d{6}$")
    new_password = serializers.CharField(max_length=128, trim_whitespace=False)


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(max_length=128, trim_whitespace=False)
    new_password = serializers.CharField(max_length=128, trim_whitespace=False)


class MeSerializer(serializers.Serializer):
    """Who is signed in and which shops they can open (own active memberships only)."""

    def to_representation(self, user):
        from apps.businesses.models import BusinessMembership

        memberships = (
            BusinessMembership.objects.filter(
                user=user, is_active=True, business__is_active=True,
                business__is_deleted=False,
            )
            .select_related("business")
            .order_by("business__name", "business_id")
        )
        deleted = (
            BusinessMembership.objects.filter(
                user=user, is_active=True, role=BusinessMembership.Role.OWNER,
                business__is_deleted=True,
            )
            .select_related("business")
            .order_by("-business__deleted_at")
        )
        return {
            "id": user.pk,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "phone_number": user.phone_number,
            "memberships": [
                {
                    "business_id": m.business_id,
                    "business_name": m.business.name,
                    "role": m.role,
                    "timezone": m.business.timezone,
                    "bill_language": m.business.bill_language,
                }
                for m in memberships
            ],
            # shops this user owns and deleted: the app offers Restore
            "deleted_shops": [
                {
                    "business_id": m.business_id,
                    "business_name": m.business.name,
                    "deleted_at": m.business.deleted_at,
                }
                for m in deleted
            ],
        }
