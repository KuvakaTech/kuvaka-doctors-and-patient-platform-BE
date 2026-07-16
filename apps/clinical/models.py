from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import BaseModel

# Re-exported — PaymentMode now lives in apps.core.money, shared with
# apps.billing/apps.finance. Kept importable from here for compatibility.
from apps.core.money import PaymentMode  # noqa: F401


class Severity(models.TextChoices):
    MILD = "mild", "Mild"
    MODERATE = "moderate", "Moderate"
    SEVERE = "severe", "Severe"


class Allergy(BaseModel):
    """A patient's known allergy — chart-level, not tied to a single visit."""

    patient = models.ForeignKey(
        "patients.PatientProfile", on_delete=models.CASCADE, related_name="allergies"
    )
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.CASCADE, related_name="+")
    substance = models.CharField(max_length=255)
    reaction = models.CharField(max_length=255, blank=True)
    severity = models.CharField(max_length=16, choices=Severity.choices, blank=True)
    noted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    def __str__(self):
        return f"Allergy<{self.patient_id}:{self.substance}>"


class ProblemStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    RESOLVED = "resolved", "Resolved"
    CHRONIC = "chronic", "Chronic"


class Problem(BaseModel):
    """A diagnosis/problem on a patient's ongoing chart (distinct from a single visit's diagnosis text)."""

    patient = models.ForeignKey(
        "patients.PatientProfile", on_delete=models.CASCADE, related_name="problems"
    )
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.CASCADE, related_name="+")
    title = models.CharField(max_length=255)
    severity = models.CharField(max_length=16, choices=Severity.choices, blank=True)
    onset_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=ProblemStatus.choices, default=ProblemStatus.ACTIVE
    )
    notes = models.TextField(blank=True)
    noted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    def __str__(self):
        return f"Problem<{self.patient_id}:{self.title}>"


class VisitType(models.TextChoices):
    FOLLOW_UP = "follow_up", "Follow-up"
    CONSULTATION = "consultation", "Consultation"
    EMERGENCY = "emergency", "Emergency"
    PROCEDURE = "procedure", "Procedure"
    WELLNESS_CHECK = "wellness_check", "Wellness Check"
    TELECONSULTATION = "teleconsultation", "Teleconsultation"


class Visit(BaseModel):
    """A single consultation/encounter. Vitals and prescriptions hang off this."""

    patient = models.ForeignKey(
        "patients.PatientProfile", on_delete=models.CASCADE, related_name="visits"
    )
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.CASCADE, related_name="visits")
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="visits_conducted"
    )
    visit_type = models.CharField(max_length=20, choices=VisitType.choices)
    visit_date = models.DateField(default=timezone.localdate)
    chief_complaint = models.TextField()
    diagnosis = models.TextField()
    recommendation = models.TextField(blank=True)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    payment_mode = models.CharField(max_length=16, choices=PaymentMode.choices, blank=True)
    # Not shown in the current consultation form UI — captured for follow-up
    # scheduling: PatientChartView surfaces this as `next_appointment`.
    next_visit_date = models.DateField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["patient", "visit_date"])]

    def __str__(self):
        return f"Visit<{self.patient_id}@{self.clinic_id}:{self.visit_date}>"


class BloodSugarType(models.TextChoices):
    FASTING = "fasting", "Fasting"
    POST_PRANDIAL = "pp", "Post-Prandial"
    RANDOM = "random", "Random"
    HBA1C = "hba1c", "HbA1c"


class Vitals(BaseModel):
    """One set of vitals recorded for a visit. Flags/BMI are computed, not stored."""

    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name="vitals")
    systolic_bp = models.PositiveSmallIntegerField(null=True, blank=True)
    diastolic_bp = models.PositiveSmallIntegerField(null=True, blank=True)
    heart_rate = models.PositiveSmallIntegerField(null=True, blank=True)
    spo2 = models.PositiveSmallIntegerField(null=True, blank=True)
    respiratory_rate = models.PositiveSmallIntegerField(null=True, blank=True)
    temperature_celsius = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    height_cm = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    blood_sugar = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    blood_sugar_type = models.CharField(max_length=16, choices=BloodSugarType.choices, blank=True)

    def __str__(self):
        return f"Vitals<visit={self.visit_id}>"

    @property
    def bmi(self) -> Decimal | None:
        if not self.weight_kg or not self.height_cm:
            return None
        height_m = self.height_cm / Decimal("100")
        return round(self.weight_kg / (height_m * height_m), 1)

    @property
    def flags(self) -> dict:
        """
        Server-side mirror of the frontend's `autoFlag()` thresholds — kept
        authoritative here since the client shouldn't be the only place
        that decides whether a reading is clinically out of range.
        """
        flags: dict[str, str] = {}
        if self.systolic_bp is not None:
            flags["systolic_bp"] = (
                "high" if self.systolic_bp > 140 else "low" if self.systolic_bp < 90 else "normal"
            )
        if self.diastolic_bp is not None:
            flags["diastolic_bp"] = (
                "high" if self.diastolic_bp > 90 else "low" if self.diastolic_bp < 60 else "normal"
            )
        if self.blood_sugar is not None:
            flags["blood_sugar"] = (
                "high" if self.blood_sugar > 140 else "low" if self.blood_sugar < 70 else "normal"
            )
        if self.heart_rate is not None:
            flags["heart_rate"] = (
                "high" if self.heart_rate > 100 else "low" if self.heart_rate < 50 else "normal"
            )
        if self.spo2 is not None:
            flags["spo2"] = "low" if self.spo2 < 95 else "normal"
        if self.temperature_celsius is not None:
            flags["temperature_celsius"] = (
                "high"
                if self.temperature_celsius > Decimal("37.5")
                else "low"
                if self.temperature_celsius < Decimal("36")
                else "normal"
            )
        return flags


class MedicineType(models.TextChoices):
    TABLET = "tablet", "Tablet"
    CAPSULE = "capsule", "Capsule"
    SYRUP = "syrup", "Syrup"
    INJECTION = "injection", "Injection"
    CREAM = "cream", "Cream"
    DROPS = "drops", "Drops"
    OTHER = "other", "Other"


class DoctorMedicine(BaseModel):
    """
    A doctor's personal prescribing formulary — private prescribing
    shortcuts (name/type/standard dosage), distinct from
    `apps.clinics.models.Medicine`, which is the shared catalog clinic
    inventory/stock is tracked against.
    """

    doctor = models.ForeignKey(
        "doctors.DoctorProfile", on_delete=models.CASCADE, related_name="medicine_formulary"
    )
    name = models.CharField(max_length=255)
    type = models.CharField(max_length=16, choices=MedicineType.choices)
    standard_dosage = models.CharField(max_length=100)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["doctor", "name"], name="unique_doctor_medicine_name")
        ]

    def __str__(self):
        return f"DoctorMedicine<{self.doctor_id}:{self.name}>"


class Prescription(BaseModel):
    """
    One prescribed medicine — either a line item under a visit (`visit`
    set), or a standalone chart entry recorded outside any visit (`visit`
    null — e.g. a medication the patient was already on before joining this
    clinic). `patient`/`clinic` are always set directly, regardless of
    which case this is, so "what is this patient currently taking" is one
    simple query instead of two different paths.
    """

    visit = models.ForeignKey(
        Visit, on_delete=models.CASCADE, null=True, blank=True, related_name="prescriptions"
    )
    patient = models.ForeignKey(
        "patients.PatientProfile", on_delete=models.CASCADE, related_name="prescriptions"
    )
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.CASCADE, related_name="+")
    # Reference to the formulary entry it was prescribed from, if any — kept
    # nullable/SET_NULL since the formulary entry may later be edited or
    # deleted, but the prescription's own snapshot fields below must remain
    # exactly what the patient was actually told to take.
    doctor_medicine = models.ForeignKey(
        DoctorMedicine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prescriptions",
    )
    medicine_name = models.CharField(max_length=255)
    dosage = models.CharField(max_length=100)
    frequency = models.CharField(max_length=100)
    duration = models.CharField(max_length=50)
    notes = models.TextField(blank=True)
    prescribed_date = models.DateField(default=timezone.localdate)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        indexes = [models.Index(fields=["patient", "clinic", "prescribed_date"])]

    def __str__(self):
        return f"Prescription<patient={self.patient_id}:{self.medicine_name}>"
