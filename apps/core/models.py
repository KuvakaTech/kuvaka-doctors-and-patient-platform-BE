import uuid

from django.conf import settings
from django.db import models


class BaseModel(models.Model):
    """
    Abstract base for all domain models in both the doctors and patients apps.

    Records are never hard-deleted (see `deleted`) and every row is traceable
    to when it was created/modified, matching auditability conventions
    expected of a healthcare-grade backend.
    """

    id = models.BigAutoField(primary_key=True)
    external_id = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    created_date = models.DateTimeField(auto_now_add=True, db_index=True)
    modified_date = models.DateTimeField(auto_now=True, db_index=True)
    deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        abstract = True
        ordering = ("-created_date",)


class AuthEvent(models.TextChoices):
    LOGIN_SUCCESS = "login_success", "Login success"
    LOGIN_FAILED = "login_failed", "Login failed"
    LOGOUT = "logout", "Logout"
    PASSWORD_RESET_REQUESTED = "password_reset_requested", "Password reset requested"
    PASSWORD_RESET_COMPLETED = "password_reset_completed", "Password reset completed"
    PASSWORD_CHANGED = "password_changed", "Password changed"
    EMAIL_VERIFIED = "email_verified", "Email verified"
    OTP_FAILED = "otp_failed", "OTP verification failed"
    ACCOUNT_LOCKED = "account_locked", "Account locked"
    TOKEN_BLACKLISTED = "token_blacklisted", "All tokens blacklisted"
    MFA_ENROLLED = "mfa_enrolled", "MFA enrolled"
    MFA_DISABLED = "mfa_disabled", "MFA disabled"
    MFA_FAILED = "mfa_failed", "MFA verification failed"
    MFA_SUCCESS = "mfa_success", "MFA verification succeeded"
    BREAK_GLASS = "break_glass", "Emergency break-glass access"


class AuditLog(models.Model):
    """
    Immutable record of security-relevant auth events.

    HIPAA § 164.312(b) requires audit controls that record and examine
    activity in systems containing ePHI. This table is the auth layer's
    contribution to that requirement.

    Records are intentionally never updated or soft-deleted — append-only.
    Do not inherit BaseModel (which adds `deleted` and `modified_date`,
    neither of which make sense for an immutable audit log).
    """

    id = models.BigAutoField(primary_key=True)
    # user may be null for failed attempts where no valid user was resolved
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    event = models.CharField(max_length=40, choices=AuthEvent.choices, db_index=True)
    # email stored separately so failed-login attempts against a non-existent
    # email are still recorded without requiring a user FK
    email = models.EmailField(blank=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)  # extra context, never credentials
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "event", "created_at"]),
        ]

    def __str__(self):
        return f"{self.event} | {self.email or self.user_id} | {self.created_at:%Y-%m-%d %H:%M:%S}"


class EmergencyAccess(models.Model):
    """
    Immutable record of every break-glass access event.

    HIPAA § 164.312(a)(2)(ii) — Emergency access procedure (required).

    When an admin needs to access a patient's record and normal access
    controls would block it, they invoke break-glass. The access is granted
    immediately but every field is logged permanently. A second admin must
    subsequently review and mark the event — this satisfies the requirement
    for post-hoc oversight without blocking time-critical emergency access.

    Records are never updated or deleted. The `reviewed_*` fields are the
    only exception — they are set once by the reviewing admin.
    """

    # Who invoked break-glass
    accessed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="break_glass_accesses",
    )
    # Which patient record was accessed
    patient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="break_glass_accessed_records",
    )
    justification = models.TextField()  # mandatory — why the access was needed
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    accessed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Post-hoc review — filled in by a second admin
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="break_glass_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)

    class Meta:
        ordering = ("-accessed_at",)
        indexes = [
            models.Index(fields=["accessed_by", "accessed_at"]),
            models.Index(fields=["patient", "accessed_at"]),
            models.Index(fields=["reviewed_by"]),
        ]

    def __str__(self):
        return (
            f"BreakGlass | by={self.accessed_by_id} "
            f"patient={self.patient_id} | {self.accessed_at:%Y-%m-%d %H:%M:%S}"
        )

    @property
    def is_reviewed(self) -> bool:
        return self.reviewed_at is not None
