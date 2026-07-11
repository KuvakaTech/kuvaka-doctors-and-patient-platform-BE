import secrets

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, serializers, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.clinics.permissions import require_membership
from apps.core.services.audit import AuthEvent, log_auth_event
from apps.core.services.lockout import clear_failed_attempts, record_failed_attempt
from apps.patients.models import (
    ConsentGrant,
    ConsentGrantStatus,
    FamilyMember,
    FamilyMemberStatus,
    PatientClinicRegistration,
    PatientProfile,
)
from apps.patients.serializers import (
    ChangePasswordSerializer,
    ConsentGrantSerializer,
    FamilyMemberCreateSerializer,
    FamilyMemberSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PatientClinicRegistrationSerializer,
    PatientLoginSerializer,
    PatientOTPRequestSerializer,
    PatientProfileSerializer,
    PatientRegisterSerializer,
    ProvisionalPatientClaimSerializer,
    ProvisionalPatientCreateSerializer,
    SetPasswordSerializer,
)
from apps.patients.services import merge_patients
from apps.users.models import OTPPurpose, User, UserType
from apps.users.otp_service import issue_and_send_otp, verify_otp
from apps.users.password_history import record_password_change
from apps.users.password_policy import validate_new_password
from apps.users.serializers import EmailOTPVerifySerializer
from apps.users.tokens import blacklist_all_tokens, issue_tokens


class PatientProfileViewSet(viewsets.ModelViewSet):
    """Patient-owned profile records; consent/record-sharing routes land here as they ship."""

    queryset = PatientProfile.objects.filter(deleted=False)
    serializer_class = PatientProfileSerializer

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)


# --- Path 1: direct email + password signup (mirrors apps.doctors) ---------


class PatientRegisterView(APIView):
    """Create a patient account with a password up front and email a verification code."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PatientRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        issue_and_send_otp(user, OTPPurpose.EMAIL_VERIFICATION)
        return Response(
            {"detail": "Registered. Check your email for a verification code."},
            status=status.HTTP_201_CREATED,
        )


class PatientVerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = EmailOTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = User.objects.get(
                email=serializer.validated_data["email"], user_type=UserType.PATIENT
            )
        except User.DoesNotExist:
            return Response({"detail": "Invalid code."}, status=status.HTTP_400_BAD_REQUEST)

        if not verify_otp(user, OTPPurpose.EMAIL_VERIFICATION, serializer.validated_data["code"]):
            return Response(
                {"detail": "Invalid or expired code."}, status=status.HTTP_400_BAD_REQUEST
            )

        user.email_verified = True
        user.save(update_fields=["email_verified"])
        log_auth_event(request, AuthEvent.EMAIL_VERIFIED, user=user, email=user.email)
        return Response({"detail": "Email verified.", **issue_tokens(user)})


class PatientLoginView(APIView):
    """Email+password login for patients who've set a password (see SetPasswordView)."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PatientLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]

        user = User.objects.filter(email=email, user_type=UserType.PATIENT).first()

        if user is None or not user.has_usable_password() or not user.check_password(password):
            if user is not None and user.has_usable_password():
                locked = record_failed_attempt(user)
                if locked:
                    log_auth_event(request, AuthEvent.ACCOUNT_LOCKED, user=user, email=email)
            log_auth_event(request, AuthEvent.LOGIN_FAILED, email=email)
            return Response(
                {"detail": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED
            )

        if user.is_locked():
            log_auth_event(
                request,
                AuthEvent.LOGIN_FAILED,
                user=user,
                email=email,
                metadata={"reason": "account_locked"},
            )
            return Response(
                {"detail": "Account is temporarily locked. Please try again later."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not user.is_active:
            log_auth_event(
                request,
                AuthEvent.LOGIN_FAILED,
                user=user,
                email=email,
                metadata={"reason": "account_disabled"},
            )
            return Response({"detail": "Account disabled."}, status=status.HTTP_403_FORBIDDEN)
        if not user.email_verified:
            log_auth_event(
                request,
                AuthEvent.LOGIN_FAILED,
                user=user,
                email=email,
                metadata={"reason": "email_not_verified"},
            )
            return Response(
                {"detail": "Email not verified. Check your inbox for a verification code."},
                status=status.HTTP_403_FORBIDDEN,
            )

        clear_failed_attempts(user)
        log_auth_event(request, AuthEvent.LOGIN_SUCCESS, user=user, email=email)
        return Response(issue_tokens(user))


# --- Path 2: passwordless email-OTP onboarding ------------------------------


class PatientOTPRequestView(APIView):
    """
    Passwordless entry point: request a login code by email. Creates the
    account on first use. Once a password has been set (see
    SetPasswordView), returning users are expected to use PatientLoginView
    instead so we're not sending an email on every login.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PatientOTPRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        existing = User.objects.filter(email=email).first()
        if existing is not None and existing.user_type != UserType.PATIENT:
            raise serializers.ValidationError(
                {"email": "This email is already registered on the doctor/clinic platform."}
            )

        user = existing
        if user is None:
            user = User.objects.create_user(
                email=email,
                full_name=serializer.validated_data.get("full_name", ""),
                user_type=UserType.PATIENT,
            )
            PatientProfile.objects.create(user=user)

        issue_and_send_otp(user, OTPPurpose.LOGIN)
        return Response({"detail": "A login code has been sent to your email."})


class PatientOTPVerifyView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = EmailOTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(
            email=serializer.validated_data["email"], user_type=UserType.PATIENT
        ).first()
        if user is None or not verify_otp(
            user, OTPPurpose.LOGIN, serializer.validated_data["code"]
        ):
            log_auth_event(
                request,
                AuthEvent.OTP_FAILED,
                email=serializer.validated_data["email"],
                metadata={"purpose": "login"},
            )
            return Response(
                {"detail": "Invalid or expired code."}, status=status.HTTP_400_BAD_REQUEST
            )

        if not user.email_verified:
            user.email_verified = True
            user.save(update_fields=["email_verified"])

        log_auth_event(
            request,
            AuthEvent.LOGIN_SUCCESS,
            user=user,
            email=user.email,
            metadata={"method": "otp"},
        )
        return Response({"password_set": user.has_usable_password(), **issue_tokens(user)})


class SetPasswordView(APIView):
    """
    Lets an OTP-onboarded patient add a password so future logins can use
    PatientLoginView instead of another OTP email. Requires the JWT issued
    by a completed OTP verify (or an existing login) — no separate
    proof-of-ownership needed.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        password = serializer.validated_data["password"]
        validate_new_password(password, request.user, field_name="password")

        request.user.set_password(password)
        request.user.save(update_fields=["password"])
        record_password_change(request.user)
        blacklist_all_tokens(request.user)
        return Response({"detail": "Password set."})


# --- Password reset (unauthenticated — forgot password) --------------------


class PatientPasswordResetRequestView(APIView):
    """Always returns 200 to avoid leaking which emails are registered."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(
            email=serializer.validated_data["email"], user_type=UserType.PATIENT
        ).first()
        if user is not None:
            issue_and_send_otp(user, OTPPurpose.PASSWORD_RESET)
            log_auth_event(
                request, AuthEvent.PASSWORD_RESET_REQUESTED, user=user, email=user.email
            )
        return Response({"detail": "If that email is registered, a reset code has been sent."})


class PatientPasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(
            email=serializer.validated_data["email"], user_type=UserType.PATIENT
        ).first()
        if user is None or not verify_otp(
            user, OTPPurpose.PASSWORD_RESET, serializer.validated_data["code"]
        ):
            log_auth_event(
                request,
                AuthEvent.OTP_FAILED,
                email=serializer.validated_data["email"],
                metadata={"purpose": "password_reset"},
            )
            return Response(
                {"detail": "Invalid or expired code."}, status=status.HTTP_400_BAD_REQUEST
            )
        new_password = serializer.validated_data["new_password"]
        validate_new_password(new_password, user)

        user.set_password(new_password)
        user.save(update_fields=["password"])
        record_password_change(user)
        blacklist_all_tokens(user)
        log_auth_event(request, AuthEvent.PASSWORD_RESET_COMPLETED, user=user, email=user.email)
        return Response({"detail": "Password reset."})


# --- Change password (authenticated — knows current password) ---------------


class PatientChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not request.user.check_password(serializer.validated_data["current_password"]):
            log_auth_event(
                request,
                AuthEvent.LOGIN_FAILED,
                user=request.user,
                email=request.user.email,
                metadata={"reason": "wrong_current_password", "action": "change_password"},
            )
            return Response(
                {"detail": "Current password is incorrect."}, status=status.HTTP_400_BAD_REQUEST
            )
        new_password = serializer.validated_data["new_password"]
        validate_new_password(new_password, request.user)

        request.user.set_password(new_password)
        request.user.save(update_fields=["password"])
        record_password_change(request.user)
        blacklist_all_tokens(request.user)
        log_auth_event(
            request, AuthEvent.PASSWORD_CHANGED, user=request.user, email=request.user.email
        )
        return Response({"detail": "Password changed."})


def _require_patient_profile(user) -> PatientProfile:
    if user.user_type != UserType.PATIENT:
        raise PermissionDenied("This action is only available to patient accounts.")
    return user.patient_profile


def _require_staff_user(user) -> None:
    if user.user_type == UserType.PATIENT:
        raise PermissionDenied("This action is only available to staff accounts.")


# ---------------------------------------------------------------------------
# Clinic registration
# ---------------------------------------------------------------------------


class PatientClinicRegistrationListCreateView(generics.ListCreateAPIView):
    """
    Staff registering a patient at their clinic (patients can register at any
    number of clinics), or a patient listing which clinics they're
    registered at.
    """

    serializer_class = PatientClinicRegistrationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.user_type == UserType.PATIENT:
            return PatientClinicRegistration.objects.filter(
                patient=user.patient_profile, deleted=False
            )
        clinic_id = self.request.query_params.get("clinic")
        if not clinic_id:
            raise ValidationError({"clinic": "Required for staff — pass ?clinic=<external_id>."})
        qs = PatientClinicRegistration.objects.filter(clinic__external_id=clinic_id, deleted=False)
        if qs.exists():
            require_membership(user, qs.first().clinic)
        return qs

    def perform_create(self, serializer):
        _require_staff_user(self.request.user)
        clinic = serializer.validated_data["clinic"]
        require_membership(self.request.user, clinic)
        serializer.save(registered_by=self.request.user)


# ---------------------------------------------------------------------------
# Provisional (staff-created) patient accounts
# ---------------------------------------------------------------------------


class ProvisionalPatientCreateView(APIView):
    """
    Staff creates a patient account on behalf of someone who can't
    self-register. Dedupes on phone number — an existing patient with the
    same number is reused (and optionally registered at the given clinic)
    rather than duplicated.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        _require_staff_user(request.user)
        serializer = ProvisionalPatientCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        clinic = data.get("clinic")
        if clinic is not None:
            require_membership(request.user, clinic)

        pin = None
        existing = User.objects.filter(phone_number=data["phone_number"]).first()
        if existing is not None:
            if existing.user_type != UserType.PATIENT:
                raise ValidationError(
                    {"phone_number": "This number is already registered on the staff platform."}
                )
            profile = existing.patient_profile
        else:
            pin = "".join(secrets.choice("0123456789") for _ in range(6))
            user = User.objects.create_user(
                phone_number=data["phone_number"],
                password=pin,
                full_name=data.get("full_name", ""),
                user_type=UserType.PATIENT,
            )
            profile = PatientProfile.objects.create(
                user=user,
                date_of_birth=data.get("date_of_birth"),
                created_by=request.user,
                is_provisional=True,
            )

        registration = None
        if clinic is not None:
            registration, _ = PatientClinicRegistration.objects.get_or_create(
                patient=profile,
                clinic=clinic,
                defaults={"registered_by": request.user, "mrn": data.get("mrn", "")},
            )

        response = {
            "patient": PatientProfileSerializer(profile).data,
            "was_existing_account": pin is None,
        }
        if pin is not None:
            response["temporary_pin"] = pin
            response["detail"] = (
                "Share this PIN with the patient — they use it with their phone number to "
                "claim the account and set their own password."
            )
        if registration is not None:
            response["clinic_registration"] = PatientClinicRegistrationSerializer(
                registration
            ).data
        return Response(response, status=status.HTTP_201_CREATED)


class ProvisionalPatientClaimView(APIView):
    """A patient claiming a staff-created account with the PIN they were handed."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ProvisionalPatientClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = User.objects.filter(
            phone_number=data["phone_number"], user_type=UserType.PATIENT
        ).first()
        if (
            user is None
            or not user.patient_profile.is_provisional
            or not user.check_password(data["pin"])
        ):
            log_auth_event(
                request,
                AuthEvent.LOGIN_FAILED,
                email="",
                metadata={"reason": "invalid_provisional_claim", "phone_number": data["phone_number"]},
            )
            return Response(
                {"detail": "Invalid phone number or PIN."}, status=status.HTTP_400_BAD_REQUEST
            )

        validate_new_password(data["new_password"], user, field_name="new_password")
        user.set_password(data["new_password"])
        user.save(update_fields=["password"])
        record_password_change(user)

        profile = user.patient_profile
        profile.is_provisional = False
        profile.claimed_at = timezone.now()
        profile.save(update_fields=["is_provisional", "claimed_at"])

        log_auth_event(request, AuthEvent.LOGIN_SUCCESS, user=user, metadata={"method": "claim"})
        return Response(issue_tokens(user))


# ---------------------------------------------------------------------------
# Family members
# ---------------------------------------------------------------------------


class FamilyMemberListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        return FamilyMemberCreateSerializer if self.request.method == "POST" else FamilyMemberSerializer

    def get_queryset(self):
        profile = _require_patient_profile(self.request.user)
        return FamilyMember.objects.filter(patient=profile, deleted=False)

    def create(self, request, *args, **kwargs):
        profile = _require_patient_profile(request.user)
        serializer = FamilyMemberCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        related_user = User.objects.filter(
            phone_number=data["related_patient_phone_number"], user_type=UserType.PATIENT
        ).first()
        if related_user is None:
            raise ValidationError(
                {"related_patient_phone_number": "No patient account found with this number."}
            )
        related_profile = related_user.patient_profile
        if related_profile.pk == profile.pk:
            raise ValidationError({"related_patient_phone_number": "Cannot link yourself."})

        link = FamilyMember.objects.create(
            patient=profile,
            related_patient=related_profile,
            relationship=data["relationship"],
            added_by=request.user,
            status=FamilyMemberStatus.PENDING,
        )
        return Response(FamilyMemberSerializer(link).data, status=status.HTTP_201_CREATED)


class FamilyMemberRespondView(APIView):
    """The related patient accepting or rejecting a family-link request."""

    permission_classes = [IsAuthenticated]

    def post(self, request, external_id, action):
        if action not in ("accept", "reject"):
            raise ValidationError({"action": "Must be 'accept' or 'reject'."})
        profile = _require_patient_profile(request.user)
        link = get_object_or_404(
            FamilyMember, external_id=external_id, related_patient=profile, deleted=False
        )
        if link.status != FamilyMemberStatus.PENDING:
            raise ValidationError({"status": "This request has already been responded to."})

        link.status = (
            FamilyMemberStatus.ACCEPTED if action == "accept" else FamilyMemberStatus.REJECTED
        )
        link.save(update_fields=["status"])
        return Response(FamilyMemberSerializer(link).data)


# ---------------------------------------------------------------------------
# Consent grants
# ---------------------------------------------------------------------------


class ConsentGrantListCreateView(generics.ListCreateAPIView):
    """
    Closed-by-default access control: a patient granting/being asked for
    access to their unified profile. A patient POSTing here creates an
    immediately-active grant; a staff member POSTing here creates a pending
    *request* the patient must approve (see ConsentGrantApproveView).
    """

    serializer_class = ConsentGrantSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.user_type == UserType.PATIENT:
            return ConsentGrant.objects.filter(patient=user.patient_profile, deleted=False)
        clinic_ids = user.clinic_memberships.filter(is_active=True, deleted=False).values_list(
            "clinic_id", flat=True
        )
        from django.db.models import Q

        return ConsentGrant.objects.filter(
            Q(grantee_user=user) | Q(grantee_clinic_id__in=clinic_ids), deleted=False
        )

    def create(self, request, *args, **kwargs):
        user = request.user
        if user.user_type == UserType.PATIENT:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            grantee_clinic = serializer.validated_data.get("grantee_clinic")
            grantee_user = serializer.validated_data.get("grantee_user")
            if bool(grantee_clinic) == bool(grantee_user):
                raise ValidationError(
                    "Specify exactly one of grantee_clinic or grantee_user."
                )
            grant = serializer.save(
                patient=user.patient_profile,
                status=ConsentGrantStatus.ACTIVE,
                granted_at=timezone.now(),
            )
            return Response(ConsentGrantSerializer(grant).data, status=status.HTTP_201_CREATED)

        # Staff/doctor requesting access — creates a pending request.
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data.get("patient"):
            raise ValidationError({"patient": "Required when requesting access as staff."})
        if not serializer.validated_data.get("reason"):
            raise ValidationError({"reason": "Required when requesting access as staff."})
        grant = serializer.save(status=ConsentGrantStatus.PENDING, requested_by=user)
        return Response(ConsentGrantSerializer(grant).data, status=status.HTTP_201_CREATED)


class ConsentGrantRespondView(APIView):
    """The patient approving, denying, or revoking a consent grant they own."""

    permission_classes = [IsAuthenticated]

    def post(self, request, external_id, action):
        if action not in ("approve", "deny", "revoke"):
            raise ValidationError({"action": "Must be 'approve', 'deny', or 'revoke'."})
        profile = _require_patient_profile(request.user)
        grant = get_object_or_404(
            ConsentGrant, external_id=external_id, patient=profile, deleted=False
        )

        if action == "approve":
            if grant.status != ConsentGrantStatus.PENDING:
                raise ValidationError({"status": "Only a pending request can be approved."})
            grant.status = ConsentGrantStatus.ACTIVE
            grant.granted_at = timezone.now()
            grant.save(update_fields=["status", "granted_at"])
        elif action == "deny":
            if grant.status != ConsentGrantStatus.PENDING:
                raise ValidationError({"status": "Only a pending request can be denied."})
            grant.status = ConsentGrantStatus.DENIED
            grant.save(update_fields=["status"])
        else:  # revoke
            if grant.status != ConsentGrantStatus.ACTIVE:
                raise ValidationError({"status": "Only an active grant can be revoked."})
            grant.status = ConsentGrantStatus.REVOKED
            grant.revoked_at = timezone.now()
            grant.save(update_fields=["status", "revoked_at"])

        return Response(ConsentGrantSerializer(grant).data)


# ---------------------------------------------------------------------------
# Duplicate-account merge
# ---------------------------------------------------------------------------


class PatientMergeSerializer(serializers.Serializer):
    primary_patient = serializers.SlugRelatedField(
        slug_field="external_id", queryset=PatientProfile.objects.filter(deleted=False)
    )
    duplicate_patient = serializers.SlugRelatedField(
        slug_field="external_id", queryset=PatientProfile.objects.filter(deleted=False)
    )
    reason = serializers.CharField()


class PatientMergeView(APIView):
    """Staff-triggered merge of a duplicate patient account into the primary one."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        _require_staff_user(request.user)
        serializer = PatientMergeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data["primary_patient"].pk == data["duplicate_patient"].pk:
            raise ValidationError({"duplicate_patient": "Cannot merge a patient into itself."})

        log = merge_patients(
            primary=data["primary_patient"],
            duplicate=data["duplicate_patient"],
            merged_by=request.user,
            reason=data["reason"],
        )
        return Response(
            {
                "detail": "Merged.",
                "primary_patient": str(log.primary_patient.external_id),
                "merged_patient": str(log.merged_patient.external_id),
            },
            status=status.HTTP_201_CREATED,
        )
