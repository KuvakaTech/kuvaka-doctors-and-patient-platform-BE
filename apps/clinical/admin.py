from django.contrib import admin

from apps.clinical.models import Allergy, DoctorMedicine, Prescription, Problem, Visit, Vitals

admin.site.register(Allergy)
admin.site.register(Problem)
admin.site.register(Visit)
admin.site.register(Vitals)
admin.site.register(DoctorMedicine)
admin.site.register(Prescription)
