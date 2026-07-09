from rest_framework import serializers


class EmailOTPVerifySerializer(serializers.Serializer):
    """Shared shape for any "email + code" verification step (doctor or patient)."""

    email = serializers.EmailField()
    code = serializers.CharField(max_length=12)

    def validate_email(self, value):
        return value.lower()
