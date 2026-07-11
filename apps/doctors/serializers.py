from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.clinics.models import Medicine
from apps.clinics.serializers import MedicineSerializer
from apps.doctors.models import DoctorProfile
from apps.users.models import User, UserType
from apps.users.password_history import record_password_change


class DoctorProfileSerializer(serializers.ModelSerializer):
    preferred_medicines = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=Medicine.objects.filter(deleted=False),
        many=True,
        required=False,
    )
    preferred_medicines_detail = MedicineSerializer(
        source="preferred_medicines", many=True, read_only=True
    )

    class Meta:
        model = DoctorProfile
        fields = (
            "external_id",
            "specialties",
            "registration_number",
            "preferred_medicines",
            "preferred_medicines_detail",
        )
        read_only_fields = ("external_id", "preferred_medicines_detail")


class DoctorRegisterSerializer(serializers.Serializer):
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
            user_type=UserType.DOCTOR,
        )
        DoctorProfile.objects.create(user=user)
        # Seed password history with the registration password itself, so a
        # later "change it back" isn't invisible to PasswordHistoryValidator.
        record_password_change(user)
        return user


class DoctorLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate_email(self, value):
        return value.lower()


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


class MFAVerifySerializer(serializers.Serializer):
    """Verify TOTP code after password-only login to complete MFA challenge."""

    mfa_token = serializers.CharField()
    totp_code = serializers.CharField(max_length=6, min_length=6)
