from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.patients.views import (
    PatientChangePasswordView,
    PatientLoginView,
    PatientOTPRequestView,
    PatientOTPVerifyView,
    PatientPasswordResetConfirmView,
    PatientPasswordResetRequestView,
    PatientProfileViewSet,
    PatientRegisterView,
    PatientVerifyEmailView,
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
    *router.urls,
]
