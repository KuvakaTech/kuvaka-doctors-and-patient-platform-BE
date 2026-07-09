from rest_framework import serializers, status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.services.audit import AuthEvent, log_auth_event
from apps.core.services.lockout import clear_failed_attempts, record_failed_attempt
from apps.patients.models import PatientProfile
from apps.patients.serializers import (
    ChangePasswordSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PatientLoginSerializer,
    PatientOTPRequestSerializer,
    PatientProfileSerializer,
    PatientRegisterSerializer,
    SetPasswordSerializer,
)
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
