from django.contrib import admin

from apps.patients.models import (
    ConsentGrant,
    FamilyMember,
    PatientClinicRegistration,
    PatientMergeLog,
    PatientProfile,
)

admin.site.register(PatientProfile)
admin.site.register(PatientClinicRegistration)
admin.site.register(FamilyMember)
admin.site.register(ConsentGrant)
admin.site.register(PatientMergeLog)
