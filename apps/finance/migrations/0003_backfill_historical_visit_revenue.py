# Data migration — backfill: apps.finance.services.record_visit_revenue
# only runs on new visit create/update from here on; this covers every
# priced visit that already existed. Every backfilled entry is unsplit
# (no RevenueShareRule could have existed before this app did) and
# attributed to whichever clinic owned the visit's own BusinessUnit —
# same derivation apps.finance.services.record_visit_revenue uses at
# runtime, duplicated here (not called directly) because historical
# models from apps.get_model don't carry real model code/methods.
from decimal import Decimal

from django.db import migrations


def backfill_visit_revenue(apps, schema_editor):
    Visit = apps.get_model("clinical", "Visit")
    RevenueEntry = apps.get_model("finance", "RevenueEntry")
    BusinessUnit = apps.get_model("finance", "BusinessUnit")

    visits = Visit.objects.filter(deleted=False, amount_paid__isnull=False)
    for visit in visits.iterator():
        if RevenueEntry.objects.filter(visit_id=visit.id).exists():
            continue

        amount = visit.amount_paid
        amount_received = Decimal("0") if visit.payment_mode == "insurance" else amount
        if amount_received <= 0:
            status = "pending"
        elif amount_received >= amount:
            status = "received"
        else:
            status = "partial"

        business_unit = BusinessUnit.objects.filter(clinic_id=visit.clinic_id).first()

        RevenueEntry.objects.create(
            doctor_id=visit.doctor_id,
            business_unit_id=business_unit.id if business_unit else None,
            clinic_id=visit.clinic_id,
            source_type="clinic_visit",
            direction="income",
            amount=amount,
            amount_received=amount_received,
            currency="INR",
            payment_mode=visit.payment_mode,
            status=status,
            occurred_on=visit.visit_date,
            settled_on=visit.visit_date if status == "received" else None,
            split_enabled=False,
            visit_id=visit.id,
            patient_id=visit.patient_id,
            recorded_by_id=visit.doctor_id,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0002_backfill_business_units"),
        ("clinical", "0002_alter_prescription_options_prescription_added_by_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_visit_revenue, noop_reverse),
    ]
