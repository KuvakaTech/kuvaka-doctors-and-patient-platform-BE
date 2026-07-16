"""
The two data migrations (0002_backfill_business_units,
0003_backfill_historical_visit_revenue) duplicate a slice of
apps.finance.services' logic rather than calling it, per Django's
documented data-migration convention (historical models from
apps.get_model don't carry real model code) — see the comments in those
migration files. Exercised here directly against the real `django.apps`
registry, which is an acceptable stand-in for the historical registry for
verifying the business logic itself (field values, not migration state
resolution).
"""

from decimal import Decimal
from importlib import import_module

import pytest
from django.apps import apps as real_apps

from apps.clinical.models import PaymentMode, Visit, VisitType
from apps.clinics.models import Clinic
from apps.finance.models import BusinessUnit, RevenueEntry
from apps.patients.models import PatientProfile
from apps.users.models import User

_business_units_migration = import_module("apps.finance.migrations.0002_backfill_business_units")
_visit_revenue_migration = import_module(
    "apps.finance.migrations.0003_backfill_historical_visit_revenue"
)


@pytest.mark.django_db
def test_backfill_business_units_creates_one_per_existing_clinic():
    doctor = User.objects.create_user(email="doc@example.com", password="pw", user_type="doctor")
    clinic = Clinic.objects.create(name="Pre-existing Clinic", owner=doctor)
    BusinessUnit.objects.all().delete()  # undo the live perform_create hook for this test

    _business_units_migration.backfill_business_units(real_apps, None)

    unit = BusinessUnit.objects.get(owner=doctor, clinic=clinic)
    assert unit.name == "Pre-existing Clinic"
    assert unit.unit_type == "clinic"


@pytest.mark.django_db
def test_backfill_business_units_is_idempotent():
    doctor = User.objects.create_user(email="doc@example.com", password="pw", user_type="doctor")
    Clinic.objects.create(name="Pre-existing Clinic", owner=doctor)

    _business_units_migration.backfill_business_units(real_apps, None)
    _business_units_migration.backfill_business_units(real_apps, None)

    assert BusinessUnit.objects.filter(owner=doctor).count() == 1


@pytest.mark.django_db
def test_backfill_visit_revenue_creates_unsplit_entries_for_historical_visits():
    doctor = User.objects.create_user(email="doc@example.com", password="pw", user_type="doctor")
    clinic = Clinic.objects.create(name="Pre-existing Clinic", owner=doctor)
    patient = PatientProfile.objects.create(
        user=User.objects.create_user(email="p@example.com", password="pw", user_type="patient")
    )
    # Simulates a Visit that existed before apps.finance did — created
    # directly, bypassing VisitListCreateView (so no live capture ran).
    visit = Visit.objects.create(
        patient=patient,
        clinic=clinic,
        doctor=doctor,
        visit_type=VisitType.CONSULTATION,
        chief_complaint="Cough",
        diagnosis="Cold",
        amount_paid=Decimal("800.00"),
        payment_mode=PaymentMode.CASH,
    )
    assert not RevenueEntry.objects.filter(visit=visit).exists()

    _visit_revenue_migration.backfill_visit_revenue(real_apps, None)

    entry = RevenueEntry.objects.get(visit=visit)
    assert entry.amount == Decimal("800.00")
    assert entry.amount_received == Decimal("800.00")
    assert entry.status == "received"
    assert entry.split_enabled is False
    assert entry.doctor_id == doctor.id


@pytest.mark.django_db
def test_backfill_visit_revenue_skips_visits_with_no_amount():
    doctor = User.objects.create_user(email="doc@example.com", password="pw", user_type="doctor")
    clinic = Clinic.objects.create(name="Pre-existing Clinic", owner=doctor)
    patient = PatientProfile.objects.create(
        user=User.objects.create_user(email="p2@example.com", password="pw", user_type="patient")
    )
    Visit.objects.create(
        patient=patient,
        clinic=clinic,
        doctor=doctor,
        visit_type=VisitType.CONSULTATION,
        chief_complaint="Cough",
        diagnosis="Cold",
    )

    _visit_revenue_migration.backfill_visit_revenue(real_apps, None)

    assert not RevenueEntry.objects.exists()


@pytest.mark.django_db
def test_backfill_visit_revenue_is_idempotent():
    doctor = User.objects.create_user(email="doc@example.com", password="pw", user_type="doctor")
    clinic = Clinic.objects.create(name="Pre-existing Clinic", owner=doctor)
    patient = PatientProfile.objects.create(
        user=User.objects.create_user(email="p3@example.com", password="pw", user_type="patient")
    )
    visit = Visit.objects.create(
        patient=patient,
        clinic=clinic,
        doctor=doctor,
        visit_type=VisitType.CONSULTATION,
        chief_complaint="Cough",
        diagnosis="Cold",
        amount_paid=Decimal("800.00"),
        payment_mode=PaymentMode.CASH,
    )

    _visit_revenue_migration.backfill_visit_revenue(real_apps, None)
    _visit_revenue_migration.backfill_visit_revenue(real_apps, None)

    assert RevenueEntry.objects.filter(visit=visit).count() == 1


@pytest.mark.django_db
def test_backfill_visit_revenue_marks_insurance_pending():
    doctor = User.objects.create_user(email="doc@example.com", password="pw", user_type="doctor")
    clinic = Clinic.objects.create(name="Pre-existing Clinic", owner=doctor)
    patient = PatientProfile.objects.create(
        user=User.objects.create_user(email="p4@example.com", password="pw", user_type="patient")
    )
    Visit.objects.create(
        patient=patient,
        clinic=clinic,
        doctor=doctor,
        visit_type=VisitType.CONSULTATION,
        chief_complaint="Cough",
        diagnosis="Cold",
        amount_paid=Decimal("1200.00"),
        payment_mode=PaymentMode.INSURANCE,
    )

    _visit_revenue_migration.backfill_visit_revenue(real_apps, None)

    entry = RevenueEntry.objects.get()
    assert entry.status == "pending"
    assert entry.amount_received == Decimal("0")
