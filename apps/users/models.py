import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.core.models import BaseModel


class UserType(models.TextChoices):
    """
    Discriminates which side of the platform an account belongs to.

    `PATIENT` accounts are extended by `apps.patients.models.PatientProfile`;
    every other type is extended by `apps.doctors.models.DoctorProfile`. One
    auth model with domain apps owning their own profile/role data, rather
    than separate user tables per app.
    """

    DOCTOR = "doctor", "Doctor"
    NURSE = "nurse", "Nurse"
    RECEPTIONIST = "receptionist", "Receptionist"
    LAB_TECHNICIAN = "lab_technician", "Lab Technician"
    PHARMACIST = "pharmacist", "Pharmacist"
    CLINIC_ADMIN = "clinic_admin", "Clinic Admin"
    PATIENT = "patient", "Patient"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email=None, phone_number=None, password=None, **extra_fields):
        if not email and not phone_number:
            raise ValueError("Users must have either an email or a phone number")
        if email:
            email = self.normalize_email(email)
        user = self.model(email=email, phone_number=phone_number, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email=None, phone_number=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, phone_number, password, **extra_fields)

    def create_superuser(self, email=None, phone_number=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("user_type", UserType.CLINIC_ADMIN)
        return self._create_user(email, phone_number, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin, BaseModel):
    email = models.EmailField(unique=True, null=True, blank=True)
    phone_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    full_name = models.CharField(max_length=255, blank=True)
    user_type = models.CharField(max_length=20, choices=UserType.choices)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)

    # Lockout fields — managed by apps.core.services.lockout
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    # TOTP / MFA fields — only relevant for doctor-side staff accounts
    totp_secret = models.CharField(
        max_length=64, blank=True
    )  # base32 secret; empty = MFA not enrolled
    totp_enabled = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        ordering = ("-created_date",)

    def __str__(self):
        return self.email or self.phone_number or str(self.external_id)

    def is_locked(self) -> bool:
        """Return True if the account is currently under a lockout period."""
        if self.locked_until is None:
            return False
        if timezone.now() < self.locked_until:
            return True
        # Lockout has expired — clear it so we don't keep checking
        User.objects.filter(pk=self.pk).update(locked_until=None, failed_login_attempts=0)
        self.locked_until = None
        self.failed_login_attempts = 0
        return False


class OTPPurpose(models.TextChoices):
    EMAIL_VERIFICATION = "email_verification", "Email Verification"
    PASSWORD_RESET = "password_reset", "Password Reset"
    LOGIN = "login", "Login"


class EmailOTP(BaseModel):
    """
    A one-time code emailed to a user for verification, password reset, or
    (patient-side) passwordless login. The code itself is never stored in
    the clear — only its hash — so a DB read can't leak a usable code.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_otps"
    )
    purpose = models.CharField(max_length=32, choices=OTPPurpose.choices)
    code_hash = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=["user", "purpose", "consumed_at"])]

    @staticmethod
    def _hash(code: str) -> str:
        # HMAC-SHA256 keyed with SECRET_KEY so a DB leak can't be reversed by
        # brute-forcing the 6-digit space (1M pre-images, trivially enumerable
        # with plain SHA-256).
        return hmac.new(
            settings.SECRET_KEY.encode(),
            code.encode(),
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def issue(cls, user, purpose: str) -> tuple["EmailOTP", str]:
        """Create a new OTP, invalidating any prior unconsumed ones for the same purpose."""
        cls.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True).update(
            consumed_at=timezone.now()
        )
        code = "".join(secrets.choice("0123456789") for _ in range(settings.OTP_LENGTH))
        otp = cls.objects.create(
            user=user,
            purpose=purpose,
            code_hash=cls._hash(code),
            expires_at=timezone.now() + timedelta(minutes=settings.OTP_EXPIRY_MINUTES),
        )
        return otp, code

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def verify_code(self, code: str) -> bool:
        """Validate `code` against this OTP, consuming it on success. Not reusable either way."""
        if self.consumed_at is not None or self.is_expired():
            return False
        if self.attempts >= settings.OTP_MAX_ATTEMPTS:
            return False

        self.attempts = models.F("attempts") + 1
        matches = secrets.compare_digest(self._hash(code), self.code_hash)
        if matches:
            self.consumed_at = timezone.now()
        self.save(update_fields=["attempts", "consumed_at"])
        return matches


class PasswordHistory(models.Model):
    """
    Stores hashed copies of a user's recent passwords so they can't be reused.

    Only the last N entries (settings.PASSWORD_HISTORY_COUNT) are kept per
    user — older rows are pruned on each password change.

    HIPAA § 164.308(a)(5)(ii)(D) addressable — password reuse prevention.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="password_history",
    )
    password_hash = models.CharField(
        max_length=255
    )  # full Django encoded hash (algorithm+salt+hash)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self):
        return f"PasswordHistory<{self.user_id}> @ {self.created_at:%Y-%m-%d %H:%M:%S}"
