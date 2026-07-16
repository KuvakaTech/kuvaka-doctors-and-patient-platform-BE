from django.conf import settings
from django.db import models

from apps.core.models import BaseModel


class DoctorProfile(BaseModel):
    """
    Extends `users.User` (user_type=doctor/nurse/receptionist/...) with
    doctor-platform-specific data. Clinic/facility onboarding, scheduling,
    encounters, prescriptions, and inventory will live in sibling modules
    under this app as they're built out — see ROADMAP.md.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="doctor_profile"
    )
    specialties = models.JSONField(default=list, blank=True)
    registration_number = models.CharField(max_length=64, blank=True)  # e.g. NPI or equivalent
    credentials = models.CharField(max_length=100, blank=True)  # free text, e.g. "MD, DM"
    licensed_state = models.CharField(max_length=100, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    preferred_medicines = models.ManyToManyField(
        "clinics.Medicine", blank=True, related_name="preferred_by_doctors"
    )

    def __str__(self):
        return f"DoctorProfile<{self.user_id}>"
