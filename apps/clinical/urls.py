from django.urls import path

from apps.clinical.views import (
    AllergyDetailView,
    AllergyListCreateView,
    DoctorMedicineDetailView,
    DoctorMedicineListCreateView,
    MedicationDetailView,
    MedicationListCreateView,
    PatientChartView,
    ProblemDetailView,
    ProblemListCreateView,
    VisitDetailView,
    VisitListCreateView,
    VisitVitalsUpdateView,
)

_patient_prefix = "clinics/<uuid:clinic_external_id>/patients/<uuid:patient_external_id>"

urlpatterns = [
    path(f"{_patient_prefix}/chart/", PatientChartView.as_view(), name="patient-chart"),
    path(
        f"{_patient_prefix}/allergies/",
        AllergyListCreateView.as_view(),
        name="allergy-list-create",
    ),
    path(
        f"{_patient_prefix}/allergies/<uuid:external_id>/",
        AllergyDetailView.as_view(),
        name="allergy-detail",
    ),
    path(
        f"{_patient_prefix}/problems/",
        ProblemListCreateView.as_view(),
        name="problem-list-create",
    ),
    path(
        f"{_patient_prefix}/problems/<uuid:external_id>/",
        ProblemDetailView.as_view(),
        name="problem-detail",
    ),
    path(
        f"{_patient_prefix}/medications/",
        MedicationListCreateView.as_view(),
        name="medication-list-create",
    ),
    path(
        f"{_patient_prefix}/medications/<uuid:external_id>/",
        MedicationDetailView.as_view(),
        name="medication-detail",
    ),
    path(f"{_patient_prefix}/visits/", VisitListCreateView.as_view(), name="visit-list-create"),
    path(
        f"{_patient_prefix}/visits/<uuid:visit_external_id>/",
        VisitDetailView.as_view(),
        name="visit-detail",
    ),
    path(
        f"{_patient_prefix}/visits/<uuid:visit_external_id>/vitals/",
        VisitVitalsUpdateView.as_view(),
        name="visit-vitals-update",
    ),
    path("medicines/", DoctorMedicineListCreateView.as_view(), name="doctor-medicine-list-create"),
    path(
        "medicines/<uuid:external_id>/",
        DoctorMedicineDetailView.as_view(),
        name="doctor-medicine-detail",
    ),
]
