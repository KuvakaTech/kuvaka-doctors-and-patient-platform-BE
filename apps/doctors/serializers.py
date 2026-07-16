from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request is not None and request.user.is_authenticated:
            self.fields["preferred_medicines"].child_relation.queryset = Medicine.objects.filter(
                deleted=False, owner_id=request.user.id
            )

    class Meta:
        model = DoctorProfile
        fields = (
            "external_id",
            "specialties",
            "registration_number",
            "credentials",
            "licensed_state",
            "preferred_medicines",
            "preferred_medicines_detail",
        )
        read_only_fields = ("external_id", "preferred_medicines_detail")


class DoctorMeSerializer(DoctorProfileSerializer):
    """DoctorProfileSerializer plus the identity fields on User, for the 'my profile' endpoint."""

    full_name = serializers.CharField(source="user.full_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    phone_number = serializers.CharField(source="user.phone_number", read_only=True)

    class Meta(DoctorProfileSerializer.Meta):
        fields = DoctorProfileSerializer.Meta.fields + ("full_name", "email", "phone_number")
        read_only_fields = DoctorProfileSerializer.Meta.read_only_fields + (
            "full_name",
            "email",
            "phone_number",
        )


class DoctorRegisterSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, validators=[validate_password])
    credentials = serializers.CharField(max_length=100, required=False, allow_blank=True)
    specialty = serializers.CharField(max_length=100, required=False, allow_blank=True)
    registration_number = serializers.CharField(
        max_length=64, required=False, allow_blank=True
    )  # NPI or local equivalent
    licensed_state = serializers.CharField(max_length=100, required=False, allow_blank=True)
    terms_accepted = serializers.BooleanField(write_only=True)

    def validate_email(self, value):
        email = value.lower()
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return email

    def validate_terms_accepted(self, value):
        if not value:
            raise serializers.ValidationError("You must accept the terms to register.")
        return value

    def create(self, validated_data):
        full_name = f"{validated_data['first_name']} {validated_data['last_name']}".strip()
        user = User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            full_name=full_name,
            user_type=UserType.DOCTOR,
        )
        specialty = validated_data.get("specialty")
        DoctorProfile.objects.create(
            user=user,
            specialties=[specialty] if specialty else [],
            registration_number=validated_data.get("registration_number", ""),
            credentials=validated_data.get("credentials", ""),
            licensed_state=validated_data.get("licensed_state", ""),
            terms_accepted_at=timezone.now(),
        )
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
