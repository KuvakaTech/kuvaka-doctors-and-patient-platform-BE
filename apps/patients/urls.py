from rest_framework.routers import DefaultRouter

from apps.patients.views import PatientProfileViewSet

router = DefaultRouter()
router.register("profiles", PatientProfileViewSet, basename="patient-profile")

urlpatterns = router.urls
