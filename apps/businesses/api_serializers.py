from rest_framework import serializers

from apps.businesses import services
from apps.businesses.models import BusinessMembership
from apps.common.serializers import RejectUnknownFieldsMixin
from apps.users.models import User
from apps.users.phone import normalize_phone

Role = BusinessMembership.Role


class StaffReadSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    phone_number = serializers.CharField(source="user.phone_number", read_only=True)
    email = serializers.CharField(source="user.email", read_only=True)
    last_login = serializers.DateTimeField(source="user.last_login", read_only=True)
    has_email = serializers.SerializerMethodField()
    can_manage = serializers.SerializerMethodField()
    can_reset_password = serializers.SerializerMethodField()

    class Meta:
        model = BusinessMembership
        fields = [
            "id", "user_id", "username", "first_name", "last_name", "phone_number",
            "email", "role", "is_active", "joined_at", "last_login", "has_email",
            "can_manage", "can_reset_password",
        ]

    def get_has_email(self, obj):
        return bool(obj.user.email)

    def _manageable(self, obj):
        request = self.context["request"]
        return (
            obj.user_id != request.user.pk
            and obj.role in services.manageable_roles(request.business_membership)
        )

    def get_can_manage(self, obj):
        return self._manageable(obj)

    def get_can_reset_password(self, obj):
        return self._manageable(obj) and services.reset_blocker(obj) is None


class StaffCreateSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    username = serializers.CharField(
        max_length=150, validators=User._meta.get_field("username").validators
    )
    role = serializers.ChoiceField(choices=[Role.MANAGER, Role.STAFF], default=Role.STAFF)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    email = serializers.EmailField(required=False, allow_blank=True, default="")

    def validate_phone_number(self, value):
        try:
            return normalize_phone(value)
        except ValueError:
            raise serializers.ValidationError("Enter a valid 10-digit Indian mobile number.")


class StaffUpdateSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    role = serializers.ChoiceField(choices=[Role.MANAGER, Role.STAFF], required=False)
    is_active = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Send role and/or is_active.")
        return attrs
