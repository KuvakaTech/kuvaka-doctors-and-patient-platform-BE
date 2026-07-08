from django.urls import include, path

urlpatterns = [
    path("core/", include("apps.core.urls")),
    path("users/", include("apps.users.urls")),
    path("doctors/", include("apps.doctors.urls")),
    path("patients/", include("apps.patients.urls")),
]
