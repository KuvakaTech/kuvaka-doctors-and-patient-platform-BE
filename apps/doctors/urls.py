from rest_framework.routers import DefaultRouter

from apps.doctors.views import DoctorProfileViewSet

router = DefaultRouter()
router.register("profiles", DoctorProfileViewSet, basename="doctor-profile")

urlpatterns = router.urls
