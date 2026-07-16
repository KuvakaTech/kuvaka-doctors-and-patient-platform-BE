from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.clinical.models import Allergy, DoctorMedicine, Prescription, Problem, Visit, Vitals
from apps.clinical.permissions import require_patient_access
from apps.clinical.serializers import (
    AllergySerializer,
    DoctorMedicineSerializer,
    PrescriptionSerializer,
    ProblemSerializer,
    VisitSerializer,
    VisitSummarySerializer,
    VisitUpdateSerializer,
    VitalsSerializer,
)
from apps.clinics.models import Clinic, PermissionFlag
from apps.clinics.permissions import ADMIN_ROLES, require_membership, require_permission
from apps.patients.models import ConsentScope, PatientProfile
from apps.patients.serializers import PatientBriefSerializer
from apps.users.models import UserType


def _compute_age(date_of_birth) -> int | None:
    if date_of_birth is None:
        return None
    today = timezone.localdate()
    years = today.year - date_of_birth.year
    if (today.month, today.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return years


def _get_clinic(external_id) -> Clinic:
    return get_object_or_404(Clinic, external_id=external_id, deleted=False)


def _get_patient(external_id) -> PatientProfile:
    return get_object_or_404(PatientProfile, external_id=external_id, deleted=False)


def _require_clinical_writer(user, clinic):
    """Allergy/Problem entries are doctor/clinic_admin-authored chart data, not delegable (yet)."""
    membership = require_membership(user, clinic)
    if membership.role not in ADMIN_ROLES:
        raise PermissionDenied("Only a doctor or clinic admin can record this.")
    return membership


class _ClinicPatientScopedView:
    """Shared clinic/patient resolution for every clinical endpoint below."""

    def get_clinic(self):
        return _get_clinic(self.kwargs["clinic_external_id"])

    def get_patient(self):
        return _get_patient(self.kwargs["patient_external_id"])


# ---------------------------------------------------------------------------
# Patient chart
# ---------------------------------------------------------------------------


class PatientChartView(_ClinicPatientScopedView, APIView):
    """
    The aggregated patient-chart view: demographics + allergies + problems +
    current medications + latest vitals + light visit history. Requires
    full consent since it surfaces everything at once — a narrower future
    endpoint could request a specific `ConsentScope` instead.

    Deliberately does NOT include each visit's full vitals/prescriptions —
    that's what GET .../visits/{id}/ is for. `visits` here stays a light
    summary so this endpoint doesn't balloon for a patient with a long
    history.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, clinic_external_id, patient_external_id):
        clinic = self.get_clinic()
        patient = self.get_patient()
        require_patient_access(request.user, clinic, patient, ConsentScope.FULL)

        visits = list(
            Visit.objects.filter(patient=patient, clinic=clinic, deleted=False)
            .select_related("vitals")
            .order_by("-visit_date")
        )
        latest_visit = visits[0] if visits else None
        latest_vitals_visit = next((v for v in visits if hasattr(v, "vitals")), None)

        registration = patient.clinic_registrations.filter(clinic=clinic, deleted=False).first()

        return Response(
            {
                "patient": PatientBriefSerializer(patient).data,
                "mrn": registration.mrn if registration else "",
                "age": _compute_age(patient.date_of_birth),
                "status": registration.status if registration else None,
                "primary_concern": latest_visit.chief_complaint if latest_visit else "",
                "last_visit_date": latest_visit.visit_date if latest_visit else None,
                "next_appointment": latest_visit.next_visit_date if latest_visit else None,
                "latest_vitals": (
                    VitalsSerializer(latest_vitals_visit.vitals).data
                    if latest_vitals_visit
                    else None
                ),
                "allergies": AllergySerializer(
                    Allergy.objects.filter(patient=patient, deleted=False), many=True
                ).data,
                "problems": ProblemSerializer(
                    Problem.objects.filter(patient=patient, deleted=False), many=True
                ).data,
                "current_medications": self._current_medications(patient, clinic),
                "visits": VisitSummarySerializer(visits, many=True).data,
            }
        )

    @staticmethod
    def _current_medications(patient, clinic):
        """
        A flattened, deduplicated view of what the patient is currently
        taking — one entry per medicine name, most recent by
        `prescribed_date` — covering both prescriptions written during a
        visit and standalone entries recorded directly on the chart (e.g.
        medications the patient was already on).
        """
        prescriptions = Prescription.objects.filter(
            patient=patient, clinic=clinic, deleted=False
        ).order_by("-prescribed_date", "-created_date")
        seen = set()
        medications = []
        for prescription in prescriptions:
            if prescription.medicine_name in seen:
                continue
            seen.add(prescription.medicine_name)
            medications.append(PrescriptionSerializer(prescription).data)
        return medications


# ---------------------------------------------------------------------------
# Allergies
# ---------------------------------------------------------------------------


class AllergyListCreateView(_ClinicPatientScopedView, generics.ListCreateAPIView):
    serializer_class = AllergySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        clinic = self.get_clinic()
        patient = self.get_patient()
        require_patient_access(self.request.user, clinic, patient, ConsentScope.ALLERGIES)
        return Allergy.objects.filter(patient=patient, deleted=False)

    def perform_create(self, serializer):
        clinic = self.get_clinic()
        patient = self.get_patient()
        _require_clinical_writer(self.request.user, clinic)
        serializer.save(patient=patient, clinic=clinic, noted_by=self.request.user)


class AllergyDetailView(_ClinicPatientScopedView, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = AllergySerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def get_queryset(self):
        clinic = self.get_clinic()
        patient = self.get_patient()
        if self.request.method in ("PUT", "PATCH", "DELETE"):
            _require_clinical_writer(self.request.user, clinic)
        else:
            require_patient_access(self.request.user, clinic, patient, ConsentScope.ALLERGIES)
        return Allergy.objects.filter(patient=patient, deleted=False)

    def perform_destroy(self, instance):
        instance.deleted = True
        instance.save(update_fields=["deleted"])


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


class ProblemListCreateView(_ClinicPatientScopedView, generics.ListCreateAPIView):
    serializer_class = ProblemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        clinic = self.get_clinic()
        patient = self.get_patient()
        require_patient_access(self.request.user, clinic, patient, ConsentScope.DIAGNOSES)
        return Problem.objects.filter(patient=patient, deleted=False)

    def perform_create(self, serializer):
        clinic = self.get_clinic()
        patient = self.get_patient()
        _require_clinical_writer(self.request.user, clinic)
        serializer.save(patient=patient, clinic=clinic, noted_by=self.request.user)


class ProblemDetailView(_ClinicPatientScopedView, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ProblemSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def get_queryset(self):
        clinic = self.get_clinic()
        patient = self.get_patient()
        if self.request.method in ("PUT", "PATCH", "DELETE"):
            _require_clinical_writer(self.request.user, clinic)
        else:
            require_patient_access(self.request.user, clinic, patient, ConsentScope.DIAGNOSES)
        return Problem.objects.filter(patient=patient, deleted=False)

    def perform_destroy(self, instance):
        instance.deleted = True
        instance.save(update_fields=["deleted"])


# ---------------------------------------------------------------------------
# Visits
# ---------------------------------------------------------------------------


class VisitListCreateView(_ClinicPatientScopedView, generics.ListCreateAPIView):
    """
    Create a visit — matches the frontend's single-page consultation form:
    vitals and prescriptions are nested in the same POST. Only a doctor
    (professional role, not clinic-membership role — see the same reasoning
    in apps.clinics.views.StaffTaskGrantListCreateView) may conduct one.
    """

    serializer_class = VisitSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        clinic = self.get_clinic()
        patient = self.get_patient()
        require_patient_access(self.request.user, clinic, patient, ConsentScope.FULL)
        return Visit.objects.filter(patient=patient, clinic=clinic, deleted=False).order_by(
            "-visit_date"
        )

    def perform_create(self, serializer):
        clinic = self.get_clinic()
        patient = self.get_patient()
        require_patient_access(self.request.user, clinic, patient, ConsentScope.FULL)
        if self.request.user.user_type != UserType.DOCTOR:
            raise PermissionDenied("Only a doctor can record a consultation.")
        visit = serializer.save(patient=patient, clinic=clinic, doctor=self.request.user)
        # Local import — apps.billing depends on apps.clinical, so
        # importing it back at module load time here would be circular.
        # This is the only visit-side capture path — the finance-only pre-cutover path
        # (record_visit_revenue) was retired once billing became universal.
        from apps.billing.services import capture_visit_charges

        capture_visit_charges(visit, request=self.request)


class VisitDetailView(_ClinicPatientScopedView, generics.RetrieveUpdateAPIView):
    """
    GET returns the full visit (vitals + prescriptions). PATCH edits only
    the visit's own top-level fields — see VisitUpdateSerializer — and is
    doctor-only, matching who may create one in the first place.
    """

    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"
    lookup_url_kwarg = "visit_external_id"

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return VisitUpdateSerializer
        return VisitSerializer

    def get_queryset(self):
        clinic = self.get_clinic()
        patient = self.get_patient()
        require_patient_access(self.request.user, clinic, patient, ConsentScope.FULL)
        is_edit = self.request.method in ("PUT", "PATCH")
        if is_edit and self.request.user.user_type != UserType.DOCTOR:
            raise PermissionDenied("Only a doctor can edit a consultation record.")
        return Visit.objects.filter(patient=patient, clinic=clinic, deleted=False)

    def perform_update(self, serializer):
        visit = serializer.save()
        from apps.billing.services import capture_visit_charges

        capture_visit_charges(visit, request=self.request)


class VisitVitalsUpdateView(_ClinicPatientScopedView, APIView):
    """
    Record/update vitals for an existing visit independently of the visit
    itself — this is what makes `PermissionFlag.ADD_VITALS` real: a nurse
    holding that flag (as a standing permission or via a StaffTaskGrant
    scoped to this patient) can record vitals ahead of the doctor's
    consultation, without needing doctor-level access to diagnosis/
    prescriptions.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request, clinic_external_id, patient_external_id, visit_external_id):
        clinic = self.get_clinic()
        patient = self.get_patient()
        require_permission(request.user, clinic, PermissionFlag.ADD_VITALS, patient=patient)
        visit = get_object_or_404(
            Visit, external_id=visit_external_id, patient=patient, clinic=clinic, deleted=False
        )

        serializer = VitalsSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        vitals, _ = Vitals.objects.update_or_create(
            visit=visit, defaults=serializer.validated_data
        )
        return Response(VitalsSerializer(vitals).data)


# ---------------------------------------------------------------------------
# Medications — standalone chart entries (not tied to a visit)
# ---------------------------------------------------------------------------


class MedicationListCreateView(_ClinicPatientScopedView, generics.ListCreateAPIView):
    """
    A patient's medications recorded directly on the chart, independent of
    any visit — e.g. something they were already taking before joining this
    clinic. Visit-created prescriptions still show up here too (this lists
    every `Prescription` for the patient at this clinic); creating through
    this endpoint always leaves `visit` null. See `PatientChartView.
    current_medications` for the deduplicated "what are they taking now"
    view built from this same data.
    """

    serializer_class = PrescriptionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        clinic = self.get_clinic()
        patient = self.get_patient()
        require_patient_access(self.request.user, clinic, patient, ConsentScope.PRESCRIPTIONS)
        return Prescription.objects.filter(patient=patient, clinic=clinic, deleted=False).order_by(
            "-prescribed_date", "-created_date"
        )

    def perform_create(self, serializer):
        clinic = self.get_clinic()
        patient = self.get_patient()
        _require_clinical_writer(self.request.user, clinic)
        serializer.save(patient=patient, clinic=clinic, visit=None, added_by=self.request.user)


class MedicationDetailView(_ClinicPatientScopedView, generics.RetrieveUpdateDestroyAPIView):
    """Edit or discontinue any medication entry — standalone or originally added via a visit."""

    serializer_class = PrescriptionSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def get_queryset(self):
        clinic = self.get_clinic()
        patient = self.get_patient()
        if self.request.method in ("PUT", "PATCH", "DELETE"):
            _require_clinical_writer(self.request.user, clinic)
        else:
            require_patient_access(self.request.user, clinic, patient, ConsentScope.PRESCRIPTIONS)
        return Prescription.objects.filter(patient=patient, clinic=clinic, deleted=False)

    def perform_destroy(self, instance):
        instance.deleted = True
        instance.save(update_fields=["deleted"])


# ---------------------------------------------------------------------------
# Doctor's personal medicine formulary
# ---------------------------------------------------------------------------


def _require_doctor_profile(user):
    if user.user_type != UserType.DOCTOR or not hasattr(user, "doctor_profile"):
        raise PermissionDenied("Only a doctor account has a prescribing formulary.")
    return user.doctor_profile


class DoctorMedicineListCreateView(generics.ListCreateAPIView):
    serializer_class = DoctorMedicineSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        doctor_profile = _require_doctor_profile(self.request.user)
        return DoctorMedicine.objects.filter(doctor=doctor_profile, deleted=False)

    def perform_create(self, serializer):
        doctor_profile = _require_doctor_profile(self.request.user)
        serializer.save(doctor=doctor_profile)


class DoctorMedicineDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = DoctorMedicineSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def get_queryset(self):
        doctor_profile = _require_doctor_profile(self.request.user)
        return DoctorMedicine.objects.filter(doctor=doctor_profile, deleted=False)

    def perform_destroy(self, instance):
        instance.deleted = True
        instance.save(update_fields=["deleted"])
