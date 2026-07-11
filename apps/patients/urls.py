from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.patients.views import (
    ConsentGrantListCreateView,
    ConsentGrantRespondView,
    FamilyMemberListCreateView,
    FamilyMemberRespondView,
    PatientChangePasswordView,
    PatientClinicRegistrationListCreateView,
    PatientLoginView,
    PatientMergeView,
    PatientOTPRequestView,
    PatientOTPVerifyView,
    PatientPasswordResetConfirmView,
    PatientPasswordResetRequestView,
    PatientProfileViewSet,
    PatientRegisterView,
    PatientVerifyEmailView,
    ProvisionalPatientClaimView,
    ProvisionalPatientCreateView,
    SetPasswordView,
)

router = DefaultRouter()
router.register("profiles", PatientProfileViewSet, basename="patient-profile")

urlpatterns = [
    path("auth/register/", PatientRegisterView.as_view(), name="patient-register"),
    path("auth/verify-email/", PatientVerifyEmailView.as_view(), name="patient-verify-email"),
    path("auth/login/", PatientLoginView.as_view(), name="patient-login"),
    path("auth/otp/request/", PatientOTPRequestView.as_view(), name="patient-otp-request"),
    path("auth/otp/verify/", PatientOTPVerifyView.as_view(), name="patient-otp-verify"),
    path("auth/set-password/", SetPasswordView.as_view(), name="patient-set-password"),
    path(
        "auth/password-reset/request/",
        PatientPasswordResetRequestView.as_view(),
        name="patient-password-reset-request",
    ),
    path(
        "auth/password-reset/confirm/",
        PatientPasswordResetConfirmView.as_view(),
        name="patient-password-reset-confirm",
    ),
    path(
        "auth/change-password/",
        PatientChangePasswordView.as_view(),
        name="patient-change-password",
    ),
    # Staff-created ("provisional") patient accounts for non-literate / offline patients
    path(
        "provisional/", ProvisionalPatientCreateView.as_view(), name="patient-provisional-create"
    ),
    path(
        "provisional/claim/",
        ProvisionalPatientClaimView.as_view(),
        name="patient-provisional-claim",
    ),
    # Clinic registration
    path(
        "clinic-registrations/",
        PatientClinicRegistrationListCreateView.as_view(),
        name="patient-clinic-registration-list-create",
    ),
    # Family members
    path(
        "family-members/", FamilyMemberListCreateView.as_view(), name="family-member-list-create"
    ),
    path(
        "family-members/<uuid:external_id>/<str:action>/",
        FamilyMemberRespondView.as_view(),
        name="family-member-respond",
    ),
    # Consent grants
    path(
        "consent-grants/",
        ConsentGrantListCreateView.as_view(),
        name="consent-grant-list-create",
    ),
    path(
        "consent-grants/<uuid:external_id>/<str:action>/",
        ConsentGrantRespondView.as_view(),
        name="consent-grant-respond",
    ),
    # Duplicate-account merge
    path("merge/", PatientMergeView.as_view(), name="patient-merge"),
    *router.urls,
]
