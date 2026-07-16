"""
Clinic/hospital billing — "what does this patient owe this clinic".

This app never imports apps.finance at module level — the bridge
runs the other way (finance imports billing lazily, function-local, to
avoid the cycle).
"""

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

from apps.core.models import BaseModel
from apps.core.money import DEFAULT_CURRENCY, PaymentMode

# ---------------------------------------------------------------------------
# PatientAccount
# ---------------------------------------------------------------------------


class AccountStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    ON_HOLD = "on_hold", "On Hold"  # e.g. dispute — blocks new invoices, not payments
    CLOSED = "closed", "Closed"


class PatientAccount(BaseModel):
    """
    The financial relationship between one patient and one clinic — all
    charges, invoices, and payments hang off this. Lazy-created on the
    first charge (a clinical registration that never bills gets no
    account) via apps.billing.services.get_or_create_account.
    """

    patient = models.ForeignKey(
        "patients.PatientProfile", on_delete=models.CASCADE, related_name="billing_accounts"
    )
    clinic = models.ForeignKey(
        "clinics.Clinic", on_delete=models.CASCADE, related_name="billing_accounts"
    )
    status = models.CharField(
        max_length=16, choices=AccountStatus.choices, default=AccountStatus.ACTIVE
    )
    # Denormalized rollups — recomputed by
    # apps.billing.services.recalculate_account() in the same transaction
    # as every charge/invoice/payment write. `calculated_at` makes
    # staleness observable rather than silent.
    total_gross = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_invoiced = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    advance_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_due = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    calculated_at = models.DateTimeField(null=True, blank=True)

    class Meta(BaseModel.Meta):
        # Must subclass BaseModel.Meta — see apps.clinics.models.Clinic for
        # why a bare `class Meta:` would silently drop inherited `ordering`.
        constraints = [
            models.UniqueConstraint(
                fields=["patient", "clinic"], name="unique_patient_clinic_account"
            )
        ]

    def __str__(self):
        return f"PatientAccount<{self.patient_id}@{self.clinic_id}>"


# ---------------------------------------------------------------------------
# ChargeItemDefinition — the price book
# ---------------------------------------------------------------------------


class ChargeCategory(models.TextChoices):
    CONSULTATION = "consultation", "Consultation"
    PROCEDURE = "procedure", "Procedure"
    SERVICE = "service", "Service"  # dressing, injection, ambulance...
    MEDICATION = "medication", "Medication"
    LAB_TEST = "lab_test", "Lab Test"  # reserved feeder — no lab-order module yet
    BED = "bed", "Bed / Room Charge"  # reserved feeder — no IPD module yet
    DEVICE = "device", "Device Usage"
    OTHER = "other", "Other"


class PriceComponentType(models.TextChoices):
    BASE = "base", "Base Price"
    DISCOUNT = "discount", "Discount"
    TAX = "tax", "Tax"
    SURCHARGE = "surcharge", "Surcharge"


class ChargeItemDefinition(BaseModel):
    """
    One priced, billable service in a clinic's tariff. VERSIONED: a price
    edit creates version N+1 and deactivates version N; charge items
    captured from a given version keep their own price snapshot, so an
    issued bill can never be rewritten by a later price change.
    """

    clinic = models.ForeignKey(
        "clinics.Clinic", on_delete=models.CASCADE, related_name="charge_definitions"
    )
    code = models.SlugField(max_length=64)  # stable across versions
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=16, choices=ChargeCategory.choices)
    # [{"type": "base", "amount": "500.00"},
    #  {"type": "discount", "code": "senior_citizen", "factor": "0.20"},
    #  {"type": "tax", "code": "gst_12", "factor": "0.12"}]
    # Serializer-validated: exactly one base component; factor XOR amount
    # per component. DjangoJSONEncoder because components carry Decimals —
    # same lesson the codebase already learned on PurchaseOrder.items
    # (stdlib json.dumps can't serialize Decimal; see clinics/migrations/0002).
    price_components = models.JSONField(default=list, encoder=DjangoJSONEncoder)
    # Global-readiness seam —
    # amounts inside price_components are denominated in this currency,
    # defaulted from the clinic's own (DEFAULT_CURRENCY today).
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    # MEDICATION definitions link the doctor's catalog entry so pharmacy
    # billing and inventory speak the same medicine identity.
    medicine = models.ForeignKey(
        "clinics.Medicine",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charge_definitions",
    )
    # Per-doctor scoping: a doctor's own consultation fee at this
    # clinic. Visit auto-capture prefers the (clinic, doctor) definition
    # over the clinic-wide default. Null = clinic-wide tariff entry.
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="charge_definitions",
    )
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)  # only the latest version is active
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta(BaseModel.Meta):
        constraints = [
            # SQL NULLs don't collide, so uniqueness needs two conditional
            # constraints: one for clinic-wide entries, one for doctor-scoped.
            models.UniqueConstraint(
                fields=["clinic", "code", "version"],
                condition=models.Q(doctor__isnull=True),
                name="unique_clinic_code_version",
            ),
            models.UniqueConstraint(
                fields=["clinic", "doctor", "code", "version"],
                condition=models.Q(doctor__isnull=False),
                name="unique_clinic_doctor_code_version",
            ),
        ]

    def __str__(self):
        scope = f"doctor={self.doctor_id}" if self.doctor_id else "clinic-wide"
        return f"ChargeItemDefinition<{self.clinic_id}:{self.code}v{self.version}:{scope}>"


# ---------------------------------------------------------------------------
# ChargeItem
# ---------------------------------------------------------------------------


class ChargeItemStatus(models.TextChoices):
    UNBILLED = "unbilled", "Unbilled"  # captured, on the account, awaiting invoice
    BILLED = "billed", "Billed"  # snapshotted into an issued invoice
    CANCELLED = "cancelled", "Cancelled"  # voidable only while UNBILLED


class ChargeItem(BaseModel):
    """
    One billable event: N units of a priced service delivered to a
    patient at a clinic. Prices are SNAPSHOTS taken from the definition
    at capture time (same philosophy as finance.RevenueShareRule).
    """

    account = models.ForeignKey(
        PatientAccount, on_delete=models.CASCADE, related_name="charge_items"
    )
    # Denormalized from account for query/permission ergonomics — derived,
    # never independently writable (same rule as finance.RevenueEntry.clinic).
    patient = models.ForeignKey(
        "patients.PatientProfile", on_delete=models.CASCADE, related_name="+"
    )
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.CASCADE, related_name="+")

    definition = models.ForeignKey(
        ChargeItemDefinition,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charge_items",
    )
    title = models.CharField(max_length=255)  # copied from definition; editable for ad-hoc charges
    category = models.CharField(max_length=16, choices=ChargeCategory.choices)
    status = models.CharField(
        max_length=16, choices=ChargeItemStatus.choices, default=ChargeItemStatus.UNBILLED
    )

    quantity = models.DecimalField(max_digits=8, decimal_places=2, default=1)
    unit_price_components = models.JSONField(
        default=list, encoder=DjangoJSONEncoder
    )  # snapshot from definition (or manual)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)  # qty x components, stored
    # Derived from definition.currency (or the clinic's default for ad-hoc
    # charges with no definition) — never independently writable.
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    override_reason = models.TextField(blank=True)  # required when price deviates from definition

    # --- clinical source links (explicit FK per source, house style) ------
    visit = models.ForeignKey(
        "clinical.Visit",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charge_items",
    )
    prescription = models.ForeignKey(
        "clinical.Prescription",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charge_items",
    )
    inventory_item = models.ForeignKey(
        "clinics.ClinicInventoryItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Phase B2+: lab_order, bed_stay, device_usage.

    # Who performed the service — drives the finance bridge: this is
    # the doctor whose RevenueShareRule applies when the money arrives.
    performer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performed_charges",
    )
    invoice = models.ForeignKey(
        "billing.Invoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charge_items",
    )
    # Field default is a same-process fallback; every real capture path
    # explicitly passes service_date=clinic_localdate(clinic) —
    # never the bare field default, same rule as finance.RevenueEntry.occurred_on.
    service_date = models.DateField(default=timezone.localdate)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    notes = models.TextField(blank=True)

    class Meta(BaseModel.Meta):
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["clinic", "service_date"]),
        ]

    def __str__(self):
        return f"ChargeItem<{self.account_id}:{self.title}:{self.total_amount}>"


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", "Draft"  # assembling; charge items still UNBILLED
    ISSUED = "issued", "Issued"  # numbered, snapshotted, locked
    PARTIALLY_PAID = "partially_paid", "Partially Paid"
    PAID = "paid", "Paid"
    CANCELLED = "cancelled", "Cancelled"  # requires reason; releases charge items


class Invoice(BaseModel):
    account = models.ForeignKey(PatientAccount, on_delete=models.CASCADE, related_name="invoices")
    patient = models.ForeignKey(
        "patients.PatientProfile", on_delete=models.CASCADE, related_name="+"
    )
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.CASCADE, related_name="invoices")
    # "<clinic-prefix>/<fiscal-year>/<seq>" e.g. "SHC/2026-27/000042" — the
    # fiscal-year window is computed from clinic.fiscal_year_start_month
    # , the prefix from clinic.invoice_prefix (falling back to a
    # name-derived default persisted the first time this clinic issues —
    # see apps.billing.services). Blank while DRAFT; assigned at ISSUE via
    # SELECT ... FOR UPDATE on a per-(clinic, fiscal-year) counter row
    # (InvoiceNumberCounter, below), using clinic_localdate(clinic) — never
    # timezone.localdate() — to decide which fiscal year "now" falls in.
    number = models.CharField(max_length=40, blank=True)
    status = models.CharField(
        max_length=20, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT
    )
    # Immutable line-item snapshot at issue — the printable truth; the
    # printed bill can never change retroactively even if the source
    # ChargeItem rows or their definitions are edited afterward.
    line_items_snapshot = models.JSONField(default=list, encoder=DjangoJSONEncoder)
    # Derived from the clinic's currency at creation — never independently
    # writable. All charge items on one invoice share a clinic,
    # hence one currency; multi-currency invoices are out of scope.
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    total_gross = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_net = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    issued_at = models.DateTimeField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    cancelled_reason = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    class Meta(BaseModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["clinic", "number"],
                condition=~models.Q(number=""),
                name="unique_clinic_invoice_number",
            )
        ]

    def __str__(self):
        return f"Invoice<{self.number or 'DRAFT'}:{self.status}>"


class InvoiceNumberCounter(BaseModel):
    """
    One row per (clinic, fiscal_year_label) — e.g. ("Sharma Clinic",
    "2026-27") — incremented under SELECT ... FOR UPDATE when an invoice
    is issued, so concurrent issues at the same clinic never collide on
    the same sequence number.
    """

    clinic = models.ForeignKey(
        "clinics.Clinic", on_delete=models.CASCADE, related_name="invoice_counters"
    )
    fiscal_year_label = models.CharField(max_length=16)  # e.g. "2026-27"
    last_sequence = models.PositiveIntegerField(default=0)

    class Meta(BaseModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["clinic", "fiscal_year_label"], name="unique_clinic_fiscal_year_counter"
            )
        ]

    def __str__(self):
        return (
            f"InvoiceNumberCounter<{self.clinic_id}:{self.fiscal_year_label}={self.last_sequence}>"
        )


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


class PaymentKind(models.TextChoices):
    PAYMENT = "payment", "Payment"
    ADVANCE = "advance", "Advance / Deposit"  # account-level, no invoice yet
    REFUND = "refund", "Refund / Credit Note"


class Payment(BaseModel):
    """
    Money movement against an account/invoice. Kept payment-shaped (one
    row per tender event), not accounting-shaped — reconciliation
    reporting derives from these rows.
    """

    account = models.ForeignKey(PatientAccount, on_delete=models.CASCADE, related_name="payments")
    invoice = models.ForeignKey(
        Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name="payments"
    )
    kind = models.CharField(
        max_length=16, choices=PaymentKind.choices, default=PaymentKind.PAYMENT
    )
    # Cash ergonomics (adopted from studied systems): patient tenders 2000
    # for a 1740 bill -> tendered=2000, returned=260, amount=1740.
    tendered_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    returned_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2
    )  # the net movement, always positive
    # Derived from account.clinic's currency — never independently
    # writable.
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    method = models.CharField(max_length=16, choices=PaymentMode.choices)
    reference_number = models.CharField(
        max_length=64, blank=True
    )  # UPI ref / card auth / cheque no
    payment_datetime = models.DateTimeField(default=timezone.now)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    notes = models.TextField(blank=True)

    class Meta(BaseModel.Meta):
        indexes = [models.Index(fields=["account", "payment_datetime"])]

    def __str__(self):
        return f"Payment<{self.account_id}:{self.kind}:{self.amount}>"


# ---------------------------------------------------------------------------
# IdempotencyKey — money POST replay-safety
# ---------------------------------------------------------------------------


class IdempotencyKey(BaseModel):
    """
    Deduplicates retried money-mutating POSTs (payments/refunds/advances).
    A client resends the same Idempotency-Key header on a network/client
    retry; if the request body matches the original (by hash), the stored
    response is replayed instead of double-posting money. A reused key
    with a *different* body is a 422 — the caller reused a key for a
    genuinely different request, which is a client bug worth surfacing
    loudly rather than silently accepting.
    """

    account = models.ForeignKey(
        PatientAccount, on_delete=models.CASCADE, related_name="idempotency_keys"
    )
    key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)  # sha256 hex of the normalized request body
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField()

    class Meta(BaseModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["account", "key"], name="unique_account_idempotency_key"
            )
        ]

    def __str__(self):
        return f"IdempotencyKey<{self.account_id}:{self.key}>"
