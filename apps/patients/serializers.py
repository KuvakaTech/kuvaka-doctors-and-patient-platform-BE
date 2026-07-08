from rest_framework import serializers

from apps.patients.models import PatientProfile


class PatientProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientProfile
        fields = ("external_id", "date_of_birth", "emergency_contact_number")
        read_only_fields = ("external_id",)
