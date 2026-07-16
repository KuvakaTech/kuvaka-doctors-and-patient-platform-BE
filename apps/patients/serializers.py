from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.clinics.models import Clinic
from apps.clinics.serializers import ClinicSerializer
from apps.patients.models import (
    BloodGroup,
    ConsentGrant,
    ConsentScope,
    FamilyMember,
    PatientClinicRegistration,
    PatientProfile,
    Sex,
)
from apps.users.models import User, UserType
from apps.users.password_history import record_password_change


class PatientProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientProfile
        fields = (
            "external_id",
            "date_of_birth",
            "sex",
            "blood_group",
            "emergency_contact_number",
            "is_provisional",
        )
        read_only_fields = ("external_id", "is_provisional")


class PatientBriefSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    phone_number = serializers.CharField(source="user.phone_number", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = PatientProfile
        fields = (
            "external_id",
            "full_name",
            "phone_number",
            "email",
            "date_of_birth",
            "sex",
            "blood_group",
        )
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Clinic registration
# ---------------------------------------------------------------------------


class PatientClinicRegistrationSerializer(serializers.ModelSerializer):
    patient = serializers.SlugRelatedField(
        slug_field="external_id", queryset=PatientProfile.objects.filter(deleted=False)
    )
    patient_detail = PatientBriefSerializer(source="patient", read_only=True)
    clinic = serializers.SlugRelatedField(
        slug_field="external_id", queryset=Clinic.objects.filter(deleted=False)
    )
    clinic_detail = ClinicSerializer(source="clinic", read_only=True)

    class Meta:
        model = PatientClinicRegistration
        fields = (
            "external_id",
            "patient",
            "patient_detail",
            "clinic",
            "clinic_detail",
            "mrn",
            "status",
        )
        read_only_fields = ("external_id", "patient_detail", "clinic_detail")


# ---------------------------------------------------------------------------
# Provisional (staff-created) patient accounts
# ---------------------------------------------------------------------------


class ProvisionalPatientCreateSerializer(serializers.Serializer):
    """
    Staff-side creation of a patient account for someone who can't
    self-register (not literate enough to use the app, no device, etc).
    `phone_number` is the dedupe key — an existing account with the same
    number is reused instead of creating a duplicate.
    """

    phone_number = serializers.CharField(max_length=15)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    sex = serializers.ChoiceField(choices=Sex.choices, required=False, allow_blank=True)
    blood_group = serializers.ChoiceField(
        choices=BloodGroup.choices, required=False, allow_blank=True
    )
    clinic = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=Clinic.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    mrn = serializers.CharField(max_length=32, required=False, allow_blank=True)

    def validate_email(self, value):
        email = value.lower()
        if email and User.objects.filter(email=email).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return email


class ProvisionalPatientClaimSerializer(serializers.Serializer):
    """A patient claiming a staff-created account: prove you hold the PIN, set a real password."""

    phone_number = serializers.CharField(max_length=15)
    pin = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])


# ---------------------------------------------------------------------------
# Family members
# ---------------------------------------------------------------------------


class FamilyMemberSerializer(serializers.ModelSerializer):
    related_patient_detail = PatientBriefSerializer(source="related_patient", read_only=True)

    class Meta:
        model = FamilyMember
        fields = (
            "external_id",
            "related_patient",
            "related_patient_detail",
            "relationship",
            "status",
        )
        read_only_fields = ("external_id", "related_patient_detail", "status")


class FamilyMemberCreateSerializer(serializers.Serializer):
    related_patient_phone_number = serializers.CharField(max_length=15)
    relationship = serializers.ChoiceField(
        choices=FamilyMember._meta.get_field("relationship").choices
    )


# ---------------------------------------------------------------------------
# Consent grants
# ---------------------------------------------------------------------------


class ConsentGrantSerializer(serializers.ModelSerializer):
    patient = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=PatientProfile.objects.filter(deleted=False),
        required=False,
    )
    patient_detail = PatientBriefSerializer(source="patient", read_only=True)
    grantee_clinic = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=Clinic.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    grantee_user = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=User.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )
    scope = serializers.ListField(
        child=serializers.ChoiceField(choices=ConsentScope.choices), allow_empty=False
    )

    class Meta:
        model = ConsentGrant
        fields = (
            "external_id",
            "patient",
            "patient_detail",
            "grantee_clinic",
            "grantee_user",
            "scope",
            "reason",
            "status",
            "granted_at",
            "expires_at",
            "revoked_at",
        )
        read_only_fields = (
            "external_id",
            "patient_detail",
            "status",
            "granted_at",
            "revoked_at",
        )


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
