from rest_framework import serializers

from apps.clinical.models import (
    Allergy,
    DoctorMedicine,
    Prescription,
    Problem,
    Visit,
    Vitals,
)


class AllergySerializer(serializers.ModelSerializer):
    class Meta:
        model = Allergy
        fields = ("external_id", "substance", "reaction", "severity", "created_date")
        read_only_fields = ("external_id", "created_date")


class ProblemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Problem
        fields = (
            "external_id",
            "title",
            "severity",
            "onset_date",
            "status",
            "notes",
            "created_date",
        )
        read_only_fields = ("external_id", "created_date")


class VitalsSerializer(serializers.ModelSerializer):
    bmi = serializers.SerializerMethodField()
    flags = serializers.SerializerMethodField()

    class Meta:
        model = Vitals
        fields = (
            "systolic_bp",
            "diastolic_bp",
            "heart_rate",
            "spo2",
            "respiratory_rate",
            "temperature_celsius",
            "weight_kg",
            "height_cm",
            "blood_sugar",
            "blood_sugar_type",
            "bmi",
            "flags",
        )

    def get_bmi(self, obj):
        return obj.bmi

    def get_flags(self, obj):
        return obj.flags


class DoctorMedicineSerializer(serializers.ModelSerializer):
    class Meta:
        model = DoctorMedicine
        fields = ("external_id", "name", "type", "standard_dosage", "notes")
        read_only_fields = ("external_id",)


class PrescriptionSerializer(serializers.ModelSerializer):
    doctor_medicine = serializers.SlugRelatedField(
        slug_field="external_id",
        queryset=DoctorMedicine.objects.filter(deleted=False),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Prescription
        fields = (
            "external_id",
            "doctor_medicine",
            "medicine_name",
            "dosage",
            "frequency",
            "duration",
            "notes",
            "prescribed_date",
        )
        read_only_fields = ("external_id",)
        extra_kwargs = {"prescribed_date": {"required": False}}


class VisitSerializer(serializers.ModelSerializer):
    """
    Writable in one shot, matching the frontend's single-page visit form:
    vitals and prescriptions are nested and created/replaced alongside the
    visit itself rather than needing separate round-trips.
    """

    vitals = VitalsSerializer(required=False)
    prescriptions = PrescriptionSerializer(many=True, required=False)
    doctor_detail = serializers.SerializerMethodField()
    patient_detail = serializers.SerializerMethodField()

    class Meta:
        model = Visit
        fields = (
            "external_id",
            "doctor_detail",
            "patient_detail",
            "visit_type",
            "visit_date",
            "chief_complaint",
            "diagnosis",
            "recommendation",
            "amount_paid",
            "payment_mode",
            "next_visit_date",
            "vitals",
            "prescriptions",
            "created_date",
        )
        read_only_fields = ("external_id", "doctor_detail", "patient_detail", "created_date")

    def get_doctor_detail(self, obj):
        return {
            "external_id": str(obj.doctor.external_id),
            "full_name": obj.doctor.full_name,
        }

    def get_patient_detail(self, obj):
        registration = obj.patient.clinic_registrations.filter(
            clinic=obj.clinic, deleted=False
        ).first()
        return {
            "external_id": str(obj.patient.external_id),
            "full_name": obj.patient.user.full_name,
            "mrn": registration.mrn if registration else "",
        }

    def create(self, validated_data):
        vitals_data = validated_data.pop("vitals", None)
        prescriptions_data = validated_data.pop("prescriptions", [])

        visit = Visit.objects.create(**validated_data)

        if vitals_data:
            Vitals.objects.create(visit=visit, **vitals_data)
        for line in prescriptions_data:
            line.setdefault("prescribed_date", visit.visit_date)
            Prescription.objects.create(
                visit=visit,
                patient=visit.patient,
                clinic=visit.clinic,
                added_by=visit.doctor,
                **line,
            )

        return visit


class VisitUpdateSerializer(serializers.ModelSerializer):
    """
    PATCH-only, top-level fields — vitals and prescriptions are edited
    through their own dedicated endpoints (VisitVitalsUpdateView,
    MedicationDetailView), not here, since DRF's ModelSerializer can't
    nested-update writable related fields without custom per-field logic.
    """

    class Meta:
        model = Visit
        fields = (
            "external_id",
            "visit_type",
            "visit_date",
            "chief_complaint",
            "diagnosis",
            "recommendation",
            "amount_paid",
            "payment_mode",
            "next_visit_date",
        )
        read_only_fields = ("external_id",)


class VisitSummarySerializer(serializers.ModelSerializer):
    """Lightweight shape for the visit-history list — no nested vitals/prescriptions."""

    class Meta:
        model = Visit
        fields = (
            "external_id",
            "visit_type",
            "visit_date",
            "chief_complaint",
            "diagnosis",
        )
        read_only_fields = fields
