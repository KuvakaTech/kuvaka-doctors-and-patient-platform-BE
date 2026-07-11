from django.contrib import admin

from apps.clinics.models import (
    Clinic,
    ClinicInventoryItem,
    ClinicStaffMembership,
    Medicine,
    PurchaseOrder,
    StaffTaskGrant,
)

admin.site.register(Clinic)
admin.site.register(ClinicStaffMembership)
admin.site.register(Medicine)
admin.site.register(ClinicInventoryItem)
admin.site.register(PurchaseOrder)
admin.site.register(StaffTaskGrant)
