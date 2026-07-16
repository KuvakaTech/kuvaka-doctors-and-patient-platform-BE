from django.urls import include, path

urlpatterns = [
    path("core/", include("apps.core.urls")),
    path("users/", include("apps.users.urls")),
    path("clinics/", include("apps.clinics.urls")),
    path("doctors/", include("apps.doctors.urls")),
    path("patients/", include("apps.patients.urls")),
    path("clinical/", include("apps.clinical.urls")),
    path("billing/", include("apps.billing.urls")),
    path("finance/", include("apps.finance.urls")),
]
