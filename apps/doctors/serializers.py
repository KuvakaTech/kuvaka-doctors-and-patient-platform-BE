from rest_framework import serializers

from apps.doctors.models import DoctorProfile


class DoctorProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = DoctorProfile
        fields = ("external_id", "specialties", "registration_number")
        read_only_fields = ("external_id",)
