from rest_framework import viewsets

from apps.patients.models import PatientProfile
from apps.patients.serializers import PatientProfileSerializer


class PatientProfileViewSet(viewsets.ModelViewSet):
    """Patient-owned profile records; consent/record-sharing routes land here as they ship."""

    queryset = PatientProfile.objects.filter(deleted=False)
    serializer_class = PatientProfileSerializer

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)
