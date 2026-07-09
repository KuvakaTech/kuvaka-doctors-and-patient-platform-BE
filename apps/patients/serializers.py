from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.patients.models import PatientProfile
from apps.users.models import User, UserType
from apps.users.password_history import record_password_change


class PatientProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientProfile
        fields = ("external_id", "date_of_birth", "emergency_contact_number")
        read_only_fields = ("external_id",)


class PatientRegisterSerializer(serializers.Serializer):
    """Direct email+password signup — the alternative to the OTP-only onboarding path."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, validators=[validate_password])
    full_name = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_email(self, value):
        email = value.lower()
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return email

    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            full_name=validated_data.get("full_name", ""),
            user_type=UserType.PATIENT,
        )
        PatientProfile.objects.create(user=user)
        # Seed password history with the registration password itself, so a
        # later "change it back" isn't invisible to PasswordHistoryValidator.
        record_password_change(user)
        return user


class PatientLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate_email(self, value):
        return value.lower()


class PatientOTPRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()
    full_name = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_email(self, value):
        return value.lower()


class SetPasswordSerializer(serializers.Serializer):
    """
    Lets a patient who onboarded via OTP-only add a password, so future
    logins don't need an email round-trip through Brevo. Requires an
    authenticated request (JWT from a completed OTP verify or prior login) —
    no separate proof-of-email-ownership needed since that JWT already
    encodes it.
    """

    password = serializers.CharField(write_only=True, validators=[validate_password])


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.lower()


class PasswordResetConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=12)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate_email(self, value):
        return value.lower()


class ChangePasswordSerializer(serializers.Serializer):
    """Authenticated password change — requires the current password as proof of identity."""

    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])
