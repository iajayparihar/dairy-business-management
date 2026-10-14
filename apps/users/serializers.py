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
