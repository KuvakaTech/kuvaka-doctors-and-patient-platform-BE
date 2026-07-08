from django.conf import settings
from django.db import models

from apps.core.models import BaseModel


class PatientProfile(BaseModel):
    """
    Extends `users.User` (user_type=patient) with the patient's unified
    profile. Consent management, record-sharing grants, and clinical history
    aggregation will live in sibling modules under this app as they're built
    out — see ROADMAP.md.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="patient_profile"
    )
    date_of_birth = models.DateField(null=True, blank=True)
    emergency_contact_number = models.CharField(max_length=15, blank=True)

    def __str__(self):
        return f"PatientProfile<{self.user_id}>"
