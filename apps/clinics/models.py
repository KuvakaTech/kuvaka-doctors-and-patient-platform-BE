from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

from apps.core.models import BaseModel
from apps.users.models import UserType

STAFF_ROLE_CHOICES = [c for c in UserType.choices if c[0] != UserType.PATIENT]


class PermissionFlag(models.TextChoices):
    """
    The closed set of delegable capabilities in the system. `permissions`
    on `ClinicStaffMembership` and `task_type` on `StaffTaskGrant` may only
    ever contain values from this list — never a free-form string — so a
    typo or an invented client-side value can't silently grant access to
    nothing (or, worse, be checked for and silently ignored).

    Every flag also has an entry in `PERMISSION_ROLE_MAP` declaring which
    roles are even eligible to hold it — see that map's docstring.
    """

    MANAGE_STAFF = "manage_staff", "Manage Staff"
    MANAGE_INVENTORY = "manage_inventory", "Manage Inventory"
    VIEW_REVENUE = "view_revenue", "View Revenue"
    EDIT_PRESCRIPTIONS = "edit_prescriptions", "Edit Prescriptions"
    ADD_VITALS = "add_vitals", "Add Vitals"
    UPLOAD_REPORTS = "upload_reports", "Upload Reports"
    UPLOAD_IMAGES = "upload_images", "Upload Images"
    VIEW_PATIENT_HISTORY = "view_patient_history", "View Patient History"
    VOICE_NOTES = "voice_notes", "Voice Notes"
    OCR = "ocr", "OCR"
    # apps.billing — capture charges, build/issue invoices, collect payments.
    MANAGE_BILLING = "manage_billing", "Manage Billing"
    # apps.billing — cancel issued invoices, post refunds. Split from
    # MANAGE_BILLING because reversing money is a higher-trust action than
    # collecting it.
    MANAGE_REFUNDS = "manage_refunds", "Manage Refunds"


# Roles (beyond CLINIC_ADMIN/DOCTOR, which always bypass permission checks —
# see apps.clinics.permissions.ADMIN_ROLES) that are eligible to hold each
# flag. A flag mapped to an empty set is admin/doctor-only and can never be
# granted to anyone else, no matter what a caller puts in `permissions`.
# This is what actually answers "only a nurse can add vitals": ADD_VITALS
# maps to {NURSE} — a receptionist can never legally hold that flag because
# every write path validates against this map before saving.
PERMISSION_ROLE_MAP: dict[str, set[str]] = {
    PermissionFlag.MANAGE_STAFF: set(),
    PermissionFlag.MANAGE_INVENTORY: {UserType.NURSE, UserType.PHARMACIST, UserType.RECEPTIONIST},
    PermissionFlag.VIEW_REVENUE: set(),
    PermissionFlag.EDIT_PRESCRIPTIONS: {UserType.PHARMACIST},
    PermissionFlag.ADD_VITALS: {UserType.NURSE},
    PermissionFlag.UPLOAD_REPORTS: {UserType.NURSE, UserType.RECEPTIONIST, UserType.LAB_TECHNICIAN},
    PermissionFlag.UPLOAD_IMAGES: {UserType.NURSE, UserType.LAB_TECHNICIAN},
    PermissionFlag.VIEW_PATIENT_HISTORY: {UserType.NURSE},
    PermissionFlag.VOICE_NOTES: {UserType.NURSE},
    PermissionFlag.OCR: {UserType.NURSE, UserType.RECEPTIONIST},
    # Billing permissions are owner-decided rather than role-restricted
    # — every staff role is
    # *eligible*; it's ClinicStaffMembership.permissions / StaffTaskGrant
    # that decides who actually holds one, same as every other flag here.
    PermissionFlag.MANAGE_BILLING: {
        UserType.NURSE,
        UserType.RECEPTIONIST,
        UserType.PHARMACIST,
        UserType.LAB_TECHNICIAN,
    },
    PermissionFlag.MANAGE_REFUNDS: {
        UserType.NURSE,
        UserType.RECEPTIONIST,
        UserType.PHARMACIST,
        UserType.LAB_TECHNICIAN,
    },
}


class ClinicSpecialty(models.TextChoices):
    PRIMARY_CARE = "primary_care", "Primary Care"
    FAMILY_MEDICINE = "family_medicine", "Family Medicine"
    CARDIOLOGY = "cardiology", "Cardiology"
    INTERNAL_MEDICINE = "internal_medicine", "Internal Medicine"
    PEDIATRICS = "pediatrics", "Pediatrics"
    URGENT_CARE = "urgent_care", "Urgent Care"
    OTHER = "other", "Other"


class Clinic(BaseModel):
    """A hospital or clinic that staff belong to and patients register at."""

    name = models.CharField(max_length=255)
    specialty = models.CharField(max_length=32, choices=ClinicSpecialty.choices, blank=True)
    registration_number = models.CharField(max_length=64, blank=True)
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=10, blank=True)
    phone_number = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)
    hours = models.CharField(max_length=255, blank=True)  # free text, e.g. "Mon-Fri 8:00 AM - 5:00 PM"
    notes = models.TextField(blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_clinics",
    )
    is_active = models.BooleanField(default=True)
    # --- Global-readiness seams ---
    # The platform is India-only today, but money-critical, clinic-scoped
    # dates (RevenueEntry.occurred_on, Invoice fiscal-year numbering,
    # billing day-book boundaries) must be computed per-clinic, not from
    # the process-wide TIME_ZONE setting (which stays India-default — it's
    # only used for admin/platform-wide display of non-clinic-scoped
    # timestamps). Stamping these two fields now, while every value is the
    # same default, avoids an ambiguous backfill the day a non-Indian
    # clinic actually onboards. Use apps.core.money.clinic_localdate(clinic)
    # rather than django.utils.timezone.localdate() for any clinic-scoped
    # business date.
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    # 1-12: which calendar month this clinic's fiscal year starts in.
    # Default 4 = April (Indian fiscal year, Apr-Mar). Drives
    # apps.billing.Invoice's "<fiscal-year>" numbering segment.
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=4)
    # The "<clinic-prefix>" segment of invoice numbers, e.g. "SHC"
    # in "SHC/2026-27/000042". Blank until the clinic sets one explicitly;
    # apps.billing derives and persists a fallback (from the clinic name)
    # the first time an invoice is issued with none set, so a number once
    # assigned never changes even if the clinic is later renamed.
    invoice_prefix = models.CharField(max_length=10, blank=True)

    class Meta(BaseModel.Meta):
        # Must subclass BaseModel.Meta, not declare a bare `class Meta:` —
        # otherwise the abstract base's `ordering = ("-created_date",)` is
        # silently dropped rather than inherited (a real Django gotcha:
        # abstract-model Meta options only carry over automatically when
        # there's no Meta declared at all on the concrete subclass).
        constraints = [
            models.CheckConstraint(
                check=models.Q(fiscal_year_start_month__gte=1, fiscal_year_start_month__lte=12),
                name="fiscal_year_start_month_valid",
            ),
        ]

    def __str__(self):
        return self.name


class ClinicStaffMembership(BaseModel):
    """
    Links a `User` (doctor/nurse/receptionist/...) to a `Clinic` with a role
    and a set of permissions. A user can hold one membership per clinic and
    work across multiple clinics via multiple memberships.
    """

    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="staff_memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="clinic_memberships"
    )
    role = models.CharField(max_length=20, choices=STAFF_ROLE_CHOICES)
    permissions = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["clinic", "user"], name="unique_clinic_staff_member")
        ]

    def __str__(self):
        return f"ClinicStaffMembership<{self.user_id}@{self.clinic_id}:{self.role}>"


class Medicine(BaseModel):
    """
    A doctor's own medicine catalog. Scoped to the doctor who owns the
    clinic it was added from (`owner`) and shared across every clinic that
    same doctor owns — not visible to other doctors' clinics.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_medicines",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255, db_index=True)
    generic_name = models.CharField(max_length=255, blank=True)
    dosage_form = models.CharField(max_length=50, blank=True)
    strength = models.CharField(max_length=50, blank=True)
    manufacturer = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return self.name


class ClinicInventoryItem(BaseModel):
    """A clinic's stock of a given medicine, tracked per batch."""

    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="inventory_items")
    medicine = models.ForeignKey(
        Medicine, on_delete=models.CASCADE, related_name="inventory_items"
    )
    batch_number = models.CharField(max_length=64, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    quantity_in_stock = models.PositiveIntegerField(default=0)
    reorder_threshold = models.PositiveIntegerField(default=0)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["clinic", "medicine", "expiry_date"])]

    def __str__(self):
        return f"ClinicInventoryItem<{self.clinic_id}:{self.medicine_id}>"

    @property
    def is_low_stock(self) -> bool:
        return self.quantity_in_stock < self.reorder_threshold


class PurchaseOrderStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ORDERED = "ordered", "Ordered"
    RECEIVED = "received", "Received"
    CANCELLED = "cancelled", "Cancelled"


class PurchaseOrder(BaseModel):
    """
    A clinic's order to a supplier for restocking medicines.

    `items` holds the line items as a JSON list ({medicine_id, quantity,
    unit_price}) rather than a separate line-item table — there's no
    per-item receiving workflow yet, so a dedicated table would have no
    reader beyond this record.
    """

    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="purchase_orders")
    supplier_name = models.CharField(max_length=255, blank=True)
    # DjangoJSONEncoder so Decimal unit_price values in each line item don't
    # blow up psycopg's json dump (stdlib json.dumps can't serialize Decimal).
    items = models.JSONField(default=list, blank=True, encoder=DjangoJSONEncoder)
    status = models.CharField(
        max_length=16, choices=PurchaseOrderStatus.choices, default=PurchaseOrderStatus.DRAFT
    )
    ordered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    ordered_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"PurchaseOrder<{self.clinic_id}:{self.status}>"


class StaffTaskGrantStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    REVOKED = "revoked", "Revoked"
    EXPIRED = "expired", "Expired"


class StaffTaskGrant(BaseModel):
    """
    A doctor delegating a specific task to a staff member — internal
    delegation within a clinic, distinct from `patients.ConsentGrant` (which
    is the patient granting access to their data).
    """

    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="task_grants")
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="task_grants_given"
    )
    grantee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="task_grants_received"
    )
    patient = models.ForeignKey(
        "patients.PatientProfile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="task_grants",
    )
    task_type = models.CharField(max_length=32, choices=PermissionFlag.choices)
    status = models.CharField(
        max_length=16, choices=StaffTaskGrantStatus.choices, default=StaffTaskGrantStatus.ACTIVE
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"StaffTaskGrant<{self.grantee_id}:{self.task_type}>"
