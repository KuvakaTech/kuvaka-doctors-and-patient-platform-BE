from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.services.audit import AuthEvent, log_auth_event
from apps.core.services.lockout import clear_failed_attempts, record_failed_attempt
from apps.doctors.models import DoctorProfile
from apps.doctors.serializers import (
    ChangePasswordSerializer,
    DoctorLoginSerializer,
    DoctorProfileSerializer,
    DoctorRegisterSerializer,
    MFAVerifySerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
)
from apps.users.models import OTPPurpose, User, UserType
from apps.users.otp_service import issue_and_send_otp, verify_otp
from apps.users.password_history import record_password_change
from apps.users.password_policy import validate_new_password
from apps.users.serializers import EmailOTPVerifySerializer
from apps.users.tokens import blacklist_all_tokens, issue_tokens
from apps.users.totp_service import (
    decode_mfa_token,
    generate_totp_secret,
    get_totp_uri,
    issue_mfa_token,
    verify_totp_code,
)


class DoctorProfileViewSet(viewsets.ModelViewSet):
    """Doctor-owned profile records; full clinic/appointment/EMR routes land here as they ship."""

    queryset = DoctorProfile.objects.filter(deleted=False)
    serializer_class = DoctorProfileSerializer

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)


class DoctorRegisterView(APIView):
    """Create a doctor-side account and email a verification code.

    The account stays unverified until the code is confirmed.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = DoctorRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        issue_and_send_otp(user, OTPPurpose.EMAIL_VERIFICATION)
        return Response(
            {"detail": "Registered. Check your email for a verification code."},
            status=status.HTTP_201_CREATED,
        )


class DoctorVerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = EmailOTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = User.objects.exclude(user_type=UserType.PATIENT).get(
                email=serializer.validated_data["email"]
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


class DoctorLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = DoctorLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]

        user = User.objects.exclude(user_type=UserType.PATIENT).filter(email=email).first()

        if user is None or not user.check_password(password):
            if user is not None:
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

        # If MFA is enrolled, issue a short-lived challenge token instead of
        # real JWT tokens. The client must complete TOTP verification at
        # /auth/mfa/verify/ to get the actual access/refresh pair.
        if user.totp_enabled:
            mfa_token = issue_mfa_token(user)
            log_auth_event(
                request,
                AuthEvent.LOGIN_SUCCESS,
                user=user,
                email=email,
                metadata={"mfa_required": True},
            )
            return Response({"mfa_required": True, "mfa_token": mfa_token})

        log_auth_event(request, AuthEvent.LOGIN_SUCCESS, user=user, email=email)
        return Response(issue_tokens(user))


class PasswordResetRequestView(APIView):
    """Always returns 200 regardless of whether the email exists.

    This avoids leaking which emails are registered.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = (
            User.objects.exclude(user_type=UserType.PATIENT)
            .filter(email=serializer.validated_data["email"])
            .first()
        )
        if user is not None:
            issue_and_send_otp(user, OTPPurpose.PASSWORD_RESET)
            log_auth_event(
                request, AuthEvent.PASSWORD_RESET_REQUESTED, user=user, email=user.email
            )
        return Response({"detail": "If that email is registered, a reset code has been sent."})


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = (
            User.objects.exclude(user_type=UserType.PATIENT)
            .filter(email=serializer.validated_data["email"])
            .first()
        )
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


class DoctorChangePasswordView(APIView):
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


# ---------------------------------------------------------------------------
# MFA — TOTP enrollment and verification
# ---------------------------------------------------------------------------


class MFAEnrollView(APIView):
    """
    Step 1 of MFA enrollment: generate a TOTP secret and return a QR code URI.
    The frontend encodes the URI into a QR code for the user to scan with their
    Authenticator app. The secret is stored on the user but totp_enabled stays
    False until the user confirms with a valid code (see MFAEnrollConfirmView).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if request.user.totp_enabled:
            return Response(
                {"detail": "MFA is already enabled on this account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        secret = generate_totp_secret()
        # Store the pending secret — not active until confirmed
        request.user.totp_secret = secret
        request.user.save(update_fields=["totp_secret"])
        uri = get_totp_uri(request.user, secret)
        return Response(
            {
                "otpauth_uri": uri,
                "secret": secret,  # shown once for manual entry fallback
                "detail": "Scan the QR code with your Authenticator app, then confirm with a code",
            }
        )


class MFAEnrollConfirmView(APIView):
    """
    Step 2 of MFA enrollment: verify the first TOTP code to confirm the user
    has successfully scanned/entered the secret. Activates MFA on success.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        totp_code = request.data.get("totp_code", "")
        if not request.user.totp_secret:
            return Response(
                {"detail": "No pending MFA enrollment. Call /auth/mfa/enroll/ first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not verify_totp_code(request.user.totp_secret, totp_code):
            log_auth_event(
                request,
                AuthEvent.MFA_FAILED,
                user=request.user,
                email=request.user.email,
                metadata={"step": "enroll_confirm"},
            )
            return Response(
                {"detail": "Invalid code. Make sure your device time is correct and try again."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        request.user.totp_enabled = True
        request.user.save(update_fields=["totp_enabled"])
        log_auth_event(
            request, AuthEvent.MFA_ENROLLED, user=request.user, email=request.user.email
        )
        return Response({"detail": "MFA enabled successfully."})


class MFAVerifyView(APIView):
    """
    Complete an MFA challenge. Called after DoctorLoginView returns
    {mfa_required: true, mfa_token: ...}. Verifies the TOTP code against
    the mfa_token and issues real JWT tokens on success.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = MFAVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_pk = decode_mfa_token(serializer.validated_data["mfa_token"])
        if user_pk is None:
            return Response(
                {"detail": "MFA session expired or invalid. Please log in again."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        user = User.objects.exclude(user_type=UserType.PATIENT).filter(pk=user_pk).first()
        if user is None or not user.totp_enabled:
            return Response(
                {"detail": "Invalid MFA session."}, status=status.HTTP_401_UNAUTHORIZED
            )

        if not verify_totp_code(user.totp_secret, serializer.validated_data["totp_code"]):
            log_auth_event(request, AuthEvent.MFA_FAILED, user=user, email=user.email or "")
            return Response(
                {"detail": "Invalid authenticator code."}, status=status.HTTP_401_UNAUTHORIZED
            )

        log_auth_event(request, AuthEvent.MFA_SUCCESS, user=user, email=user.email or "")
        return Response(issue_tokens(user))


class MFADisableView(APIView):
    """
    Disable MFA on the account. Requires a valid TOTP code as confirmation
    so a stolen JWT alone can't be used to strip MFA protection.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        totp_code = request.data.get("totp_code", "")
        if not request.user.totp_enabled:
            return Response(
                {"detail": "MFA is not enabled on this account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not verify_totp_code(request.user.totp_secret, totp_code):
            log_auth_event(
                request,
                AuthEvent.MFA_FAILED,
                user=request.user,
                email=request.user.email,
                metadata={"step": "disable"},
            )
            return Response(
                {"detail": "Invalid authenticator code."}, status=status.HTTP_400_BAD_REQUEST
            )
        request.user.totp_enabled = False
        request.user.totp_secret = ""
        request.user.save(update_fields=["totp_enabled", "totp_secret"])
        log_auth_event(
            request, AuthEvent.MFA_DISABLED, user=request.user, email=request.user.email
        )
        return Response({"detail": "MFA disabled."})
