from rest_framework import viewsets

from apps.doctors.models import DoctorProfile
from apps.doctors.serializers import DoctorProfileSerializer


class DoctorProfileViewSet(viewsets.ModelViewSet):
    """Doctor-owned profile records; full clinic/appointment/EMR routes land here as they ship."""

    queryset = DoctorProfile.objects.filter(deleted=False)
    serializer_class = DoctorProfileSerializer

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)
