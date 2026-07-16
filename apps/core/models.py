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


class FinancialEvent(models.TextChoices):
    """
    Every state-changing action in apps.finance/apps.billing. Deliberately
    one shared vocabulary rather than one enum per app — a single audit
    query across both money apps (e.g. "everything that touched invoice X's
    money") shouldn't need to union two tables with different event shapes.
    """

    # finance
    ENTRY_CREATED = "entry_created", "Revenue entry created"
    ENTRY_UPDATED = "entry_updated", "Revenue entry updated"
    ENTRY_SETTLED = "entry_settled", "Revenue entry settled"
    ENTRY_CANCELLED = "entry_cancelled", "Revenue entry cancelled"
    SHARE_RULE_CREATED = "share_rule_created", "Revenue share rule created"
    SHARE_RULE_UPDATED = "share_rule_updated", "Revenue share rule updated"
    GRANT_CREATED = "grant_created", "Finance access grant created"
    GRANT_REVOKED = "grant_revoked", "Finance access grant revoked"
    # billing
    CHARGE_CAPTURED = "charge_captured", "Charge item captured"
    CHARGE_CANCELLED = "charge_cancelled", "Charge item cancelled"
    INVOICE_ISSUED = "invoice_issued", "Invoice issued"
    INVOICE_CANCELLED = "invoice_cancelled", "Invoice cancelled"
    PAYMENT_POSTED = "payment_posted", "Payment posted"
    REFUND_POSTED = "refund_posted", "Refund posted"
    ADVANCE_POSTED = "advance_posted", "Advance posted"
    ADVANCE_APPLIED = "advance_applied", "Advance applied to invoice"
    PRICE_DEFINITION_CHANGED = "price_definition_changed", "Price book definition changed"


class FinancialAuditLog(models.Model):
    """
    Immutable record of every money-mutating action across apps.finance and
    apps.billing. The money counterpart of AuditLog (auth events) — kept as
    a separate model/table rather than folded into AuditLog because the
    two audiences (auth security review vs financial review) query it
    differently, and mixing FinancialEvent into AuthEvent would make
    AuditLog's `event` choices span two unrelated domains.

    Records are intentionally never updated or soft-deleted — append-only,
    same convention as AuditLog. Do not inherit BaseModel (adds `deleted`
    and `modified_date`, neither of which make sense here).
    """

    id = models.BigAutoField(primary_key=True)
    # actor may be null only for system-initiated writes (e.g. a scheduled
    # backfill/migration) — every user-initiated action always has one.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="financial_audit_logs",
    )
    event = models.CharField(max_length=40, choices=FinancialEvent.choices, db_index=True)
    clinic = models.ForeignKey(
        "clinics.Clinic",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    # The mutated object — external_id, never the internal PK, matching the
    # platform-wide rule that internal ids are never persisted/exposed
    # outside their own app.
    object_type = models.CharField(max_length=40)  # e.g. "revenue_entry", "invoice"
    object_id = models.CharField(max_length=64)  # the object's external_id
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # Before/after amounts, reasons, etc. NEVER credentials, and never
    # patient names or clinical content — identifiers only.
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["clinic", "event", "created_at"]),
            models.Index(fields=["actor", "created_at"]),
        ]

    def __str__(self):
        when = self.created_at.strftime("%Y-%m-%d %H:%M:%S")
        return f"{self.event} | {self.object_type}:{self.object_id} | {when}"
