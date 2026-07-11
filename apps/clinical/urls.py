from django.urls import path

from apps.clinical.views import (
    AllergyListCreateView,
    DoctorMedicineDetailView,
    DoctorMedicineListCreateView,
    PatientChartView,
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
        f"{_patient_prefix}/problems/",
        ProblemListCreateView.as_view(),
        name="problem-list-create",
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
