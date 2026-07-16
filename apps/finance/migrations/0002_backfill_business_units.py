# Data migration — backfill: apps.clinics.views.ClinicViewSet.perform_create
# auto-creates a BusinessUnit for every NEW clinic from now on; this covers
# every clinic that already existed before apps.finance did.
#
# Uses historical models (apps.get_model) rather than importing the real
# model classes, per Django's documented data-migration convention — the
# real classes may drift from this migration's schema state over time.
from django.db import migrations


def backfill_business_units(apps, schema_editor):
    Clinic = apps.get_model("clinics", "Clinic")
    BusinessUnit = apps.get_model("finance", "BusinessUnit")

    for clinic in Clinic.objects.filter(deleted=False, owner__isnull=False):
        BusinessUnit.objects.get_or_create(
            owner_id=clinic.owner_id,
            clinic_id=clinic.id,
            defaults={
                "name": clinic.name,
                "unit_type": "clinic",
                "ownership": "owned",
            },
        )


def noop_reverse(apps, schema_editor):
    # Not worth reversing — a future migrate-back would just re-backfill
    # on the next forward run.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0001_initial"),
        ("clinics", "0007_clinic_fiscal_year_start_month_clinic_timezone_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_business_units, noop_reverse),
    ]
