from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.doctors.views import (
    DoctorChangePasswordView,
    DoctorLoginView,
    DoctorProfileViewSet,
    DoctorRegisterView,
    DoctorVerifyEmailView,
    MFADisableView,
    MFAEnrollConfirmView,
    MFAEnrollView,
    MFAVerifyView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
)

router = DefaultRouter()
router.register("profiles", DoctorProfileViewSet, basename="doctor-profile")

urlpatterns = [
    path("auth/register/", DoctorRegisterView.as_view(), name="doctor-register"),
    path("auth/verify-email/", DoctorVerifyEmailView.as_view(), name="doctor-verify-email"),
    path("auth/login/", DoctorLoginView.as_view(), name="doctor-login"),
    path(
        "auth/password-reset/request/",
        PasswordResetRequestView.as_view(),
        name="doctor-password-reset-request",
    ),
    path(
        "auth/password-reset/confirm/",
        PasswordResetConfirmView.as_view(),
        name="doctor-password-reset-confirm",
    ),
    path(
        "auth/change-password/",
        DoctorChangePasswordView.as_view(),
        name="doctor-change-password",
    ),
    # MFA
    path("auth/mfa/enroll/", MFAEnrollView.as_view(), name="doctor-mfa-enroll"),
    path(
        "auth/mfa/enroll/confirm/",
        MFAEnrollConfirmView.as_view(),
        name="doctor-mfa-enroll-confirm",
    ),
    path("auth/mfa/verify/", MFAVerifyView.as_view(), name="doctor-mfa-verify"),
    path("auth/mfa/disable/", MFADisableView.as_view(), name="doctor-mfa-disable"),
    *router.urls,
]
