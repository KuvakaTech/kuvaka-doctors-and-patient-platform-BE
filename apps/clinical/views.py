from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.clinical.models import Allergy, DoctorMedicine, Problem, Visit, Vitals
from apps.clinical.permissions import require_patient_access
from apps.clinical.serializers import (
    AllergySerializer,
    DoctorMedicineSerializer,
    ProblemSerializer,
    VisitSerializer,
    VisitSummarySerializer,
    VitalsSerializer,
)
from apps.clinics.models import Clinic, PermissionFlag
from apps.clinics.permissions import ADMIN_ROLES, require_membership, require_permission
from apps.patients.models import ConsentScope, PatientProfile
from apps.patients.serializers import PatientBriefSerializer
from apps.users.models import UserType


def _get_clinic(external_id) -> Clinic:
    return get_object_or_404(Clinic, external_id=external_id, deleted=False)


def _get_patient(external_id) -> PatientProfile:
    return get_object_or_404(PatientProfile, external_id=external_id, deleted=False)


def _require_clinical_writer(user, clinic):
    """Allergy/Problem entries are doctor/clinic_admin-authored chart data — not delegable (yet)."""
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
    recent visit history. Requires full consent since it surfaces
    everything at once — a narrower future endpoint could request a
    specific `ConsentScope` instead.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, clinic_external_id, patient_external_id):
        clinic = self.get_clinic()
        patient = self.get_patient()
        require_patient_access(request.user, clinic, patient, ConsentScope.FULL)

        visits = Visit.objects.filter(patient=patient, clinic=clinic, deleted=False).order_by(
            "-visit_date"
        )
        return Response(
            {
                "patient": PatientBriefSerializer(patient).data,
                "allergies": AllergySerializer(
                    Allergy.objects.filter(patient=patient, deleted=False), many=True
                ).data,
                "problems": ProblemSerializer(
                    Problem.objects.filter(patient=patient, deleted=False), many=True
                ).data,
                "visits": VisitSummarySerializer(visits, many=True).data,
            }
        )


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
        serializer.save(patient=patient, clinic=clinic, doctor=self.request.user)


class VisitDetailView(_ClinicPatientScopedView, generics.RetrieveAPIView):
    serializer_class = VisitSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"
    lookup_url_kwarg = "visit_external_id"

    def get_queryset(self):
        clinic = self.get_clinic()
        patient = self.get_patient()
        require_patient_access(self.request.user, clinic, patient, ConsentScope.FULL)
        return Visit.objects.filter(patient=patient, clinic=clinic, deleted=False)


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
