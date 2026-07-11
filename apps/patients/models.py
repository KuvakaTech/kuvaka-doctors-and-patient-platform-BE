from django.conf import settings
from django.db import models

from apps.core.models import BaseModel


class Sex(models.TextChoices):
    MALE = "M", "Male"
    FEMALE = "F", "Female"
    OTHER = "Other", "Other"


class BloodGroup(models.TextChoices):
    A_POSITIVE = "A+", "A+"
    A_NEGATIVE = "A-", "A-"
    B_POSITIVE = "B+", "B+"
    B_NEGATIVE = "B-", "B-"
    AB_POSITIVE = "AB+", "AB+"
    AB_NEGATIVE = "AB-", "AB-"
    O_POSITIVE = "O+", "O+"
    O_NEGATIVE = "O-", "O-"


class PatientProfile(BaseModel):
    """
    Extends `users.User` (user_type=patient) with the patient's unified
    profile. Consent management, record-sharing grants, and clinical history
    aggregation will live in sibling modules under this app as they're built
    out — see ROADMAP.md.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="patient_profile"
    )
    date_of_birth = models.DateField(null=True, blank=True)
    emergency_contact_number = models.CharField(max_length=15, blank=True)
    sex = models.CharField(max_length=8, choices=Sex.choices, blank=True)
    blood_group = models.CharField(max_length=3, choices=BloodGroup.choices, blank=True)

    # Set when a staff member (doctor/receptionist) creates this account on
    # behalf of a patient who can't self-register — e.g. not literate enough
    # to use the app, or walked in without a device. `is_provisional` stays
    # True until the patient claims the account via phone + OTP and sets
    # their own credential.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="patients_created",
    )
    is_provisional = models.BooleanField(default=False)
    claimed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"PatientProfile<{self.user_id}>"


class PatientClinicStatus(models.TextChoices):
    STABLE = "stable", "Stable"
    MONITORING = "monitoring", "Monitoring"
    CRITICAL = "critical", "Critical"
    DISCHARGED = "discharged", "Discharged"
    SCHEDULED = "scheduled", "Scheduled"


class PatientClinicRegistration(BaseModel):
    """
    A patient registering at a specific clinic — patients can register at
    any number of clinics.

    `status` is this patient's current care status *at this clinic*
    specifically (not a global property of the patient — the same person
    can be "critical" at one clinic and "discharged" at another, since
    they're independent episodes of care). Set manually by a doctor for now;
    automatic derivation from vitals/problems can layer on top later.
    """

    patient = models.ForeignKey(
        PatientProfile, on_delete=models.CASCADE, related_name="clinic_registrations"
    )
    clinic = models.ForeignKey(
        "clinics.Clinic", on_delete=models.CASCADE, related_name="patient_registrations"
    )
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    mrn = models.CharField(max_length=32, blank=True)  # clinic-local medical record number
    status = models.CharField(
        max_length=16, choices=PatientClinicStatus.choices, default=PatientClinicStatus.STABLE
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["patient", "clinic"], name="unique_patient_clinic_registration"
            )
        ]

    def __str__(self):
        return f"PatientClinicRegistration<{self.patient_id}@{self.clinic_id}>"


class FamilyRelationship(models.TextChoices):
    SPOUSE = "spouse", "Spouse"
    CHILD = "child", "Child"
    PARENT = "parent", "Parent"
    SIBLING = "sibling", "Sibling"
    GUARDIAN = "guardian", "Guardian"
    OTHER = "other", "Other"


class FamilyMemberStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"


class FamilyMember(BaseModel):
    """
    Links two real patient accounts as family members. `related_patient`
    must accept the link (`status`) before it's considered active — a
    patient shouldn't be able to unilaterally attach someone else's account.
    """

    patient = models.ForeignKey(
        PatientProfile, on_delete=models.CASCADE, related_name="family_members"
    )
    related_patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="+")
    relationship = models.CharField(max_length=16, choices=FamilyRelationship.choices)
    status = models.CharField(
        max_length=16, choices=FamilyMemberStatus.choices, default=FamilyMemberStatus.PENDING
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["patient", "related_patient"], name="unique_family_member_link"
            ),
            models.CheckConstraint(
                check=~models.Q(patient=models.F("related_patient")),
                name="family_member_not_self",
            ),
        ]

    def __str__(self):
        return f"FamilyMember<{self.patient_id}~{self.related_patient_id}:{self.relationship}>"


class ConsentGrantStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACTIVE = "active", "Active"
    REVOKED = "revoked", "Revoked"
    DENIED = "denied", "Denied"
    EXPIRED = "expired", "Expired"


class ConsentScope(models.TextChoices):
    """
    The closed set of record categories a patient can grant access to.
    `ConsentGrant.scope` may only ever contain values from this list —
    mirrors `apps.clinics.models.PermissionFlag` for the same reason: a
    free-form string field has no way to reject a typo or an invented
    value, and a future clinical-data endpoint checking `"diagnosis" in
    scope` when every grant actually stored `"diagnoses"` would silently
    deny access to everyone.
    """

    DIAGNOSES = "diagnoses", "Diagnoses"
    ALLERGIES = "allergies", "Allergies"
    MEDICATIONS = "medications", "Medications (current & past)"
    CHRONIC_CONDITIONS = "chronic_conditions", "Chronic Conditions"
    SURGERIES = "surgeries", "Surgeries"
    FAMILY_HISTORY = "family_history", "Family History"
    VACCINATIONS = "vaccinations", "Vaccinations"
    VITALS = "vitals", "Vitals"
    LAB_REPORTS = "lab_reports", "Lab Reports"
    IMAGING_REPORTS = "imaging_reports", "Imaging Reports"
    DISCHARGE_SUMMARIES = "discharge_summaries", "Discharge Summaries"
    PRESCRIPTIONS = "prescriptions", "Prescriptions"
    AI_SUMMARIES = "ai_summaries", "AI Summaries"
    CONVERSATION_TRANSCRIPTS = "conversation_transcripts", "Conversation Transcripts"
    AUDIO_RECORDINGS = "audio_recordings", "Audio Recordings"
    UPLOADED_DOCUMENTS = "uploaded_documents", "Uploaded Documents"
    REFERRALS = "referrals", "Referrals"
    FULL = "full", "Full Profile"


class ConsentGrant(BaseModel):
    """
    A patient granting a doctor or clinic access to (some or all of) their
    unified profile. Closed by default — no grant, no cross-clinic data.

    Doubles as the access-request record: a doctor/clinic can create one
    with status=PENDING and `requested_by` set; the patient approving it
    flips status to ACTIVE. This avoids a separate request table for what
    is otherwise the same lifecycle.
    """

    patient = models.ForeignKey(
        PatientProfile, on_delete=models.CASCADE, related_name="consent_grants"
    )
    grantee_clinic = models.ForeignKey(
        "clinics.Clinic",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="consent_grants",
    )
    grantee_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="consent_grants_received",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    scope = models.JSONField(default=list, blank=True)
    reason = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=ConsentGrantStatus.choices, default=ConsentGrantStatus.PENDING
    )
    granted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(grantee_clinic__isnull=False, grantee_user__isnull=True)
                    | models.Q(grantee_clinic__isnull=True, grantee_user__isnull=False)
                ),
                name="consent_grant_exactly_one_grantee",
            )
        ]
        indexes = [
            models.Index(fields=["patient", "status"]),
            models.Index(fields=["grantee_user", "status"]),
            models.Index(fields=["grantee_clinic", "status"]),
        ]

    def __str__(self):
        grantee = self.grantee_clinic_id or self.grantee_user_id
        return f"ConsentGrant<{self.patient_id}->{grantee}:{self.status}>"


class PatientMergeLog(models.Model):
    """
    Immutable record of a duplicate-patient merge. Follows the same
    append-only convention as `apps.core.models.AuditLog` — records here are
    never updated or deleted.
    """

    id = models.BigAutoField(primary_key=True)
    primary_patient = models.ForeignKey(
        PatientProfile, on_delete=models.SET_NULL, null=True, related_name="merge_absorbed"
    )
    merged_patient = models.ForeignKey(
        PatientProfile, on_delete=models.SET_NULL, null=True, related_name="merge_source"
    )
    merged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"PatientMergeLog<{self.merged_patient_id}->{self.primary_patient_id}>"
