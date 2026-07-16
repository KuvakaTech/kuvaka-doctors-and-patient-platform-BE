"""
Global-readiness seams

The platform is India-only today; these fields/validators exist so a
future non-Indian clinic's money is correctly dated from day one, without
an ambiguous backfill. See apps.core.money.clinic_localdate for the
computation these fields feed.
"""

import pytest
from django.db import IntegrityError, transaction

from apps.clinics.models import Clinic
from apps.clinics.serializers import ClinicSerializer
from apps.users.models import User


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@example.com", password="pw", user_type="doctor")


@pytest.mark.django_db
def test_fiscal_year_start_month_rejects_out_of_range_at_db_level(owner):
    with pytest.raises(IntegrityError), transaction.atomic():
        Clinic.objects.create(name="Bad Clinic", owner=owner, fiscal_year_start_month=13)


@pytest.mark.django_db
def test_fiscal_year_start_month_accepts_valid_boundary_values(owner):
    jan = Clinic.objects.create(name="Jan FY", owner=owner, fiscal_year_start_month=1)
    dec = Clinic.objects.create(name="Dec FY", owner=owner, fiscal_year_start_month=12)
    assert jan.fiscal_year_start_month == 1
    assert dec.fiscal_year_start_month == 12


@pytest.mark.django_db
def test_serializer_rejects_unknown_timezone(owner):
    serializer = ClinicSerializer(
        Clinic.objects.create(name="Test Clinic", owner=owner),
        data={"timezone": "Mars/Olympus_Mons"},
        partial=True,
    )
    assert not serializer.is_valid()
    assert "timezone" in serializer.errors


@pytest.mark.django_db
def test_serializer_accepts_valid_iana_timezone(owner):
    serializer = ClinicSerializer(
        Clinic.objects.create(name="Test Clinic", owner=owner),
        data={"timezone": "America/New_York"},
        partial=True,
    )
    assert serializer.is_valid(), serializer.errors


@pytest.mark.django_db
def test_serializer_rejects_out_of_range_fiscal_year_start_month(owner):
    serializer = ClinicSerializer(
        Clinic.objects.create(name="Test Clinic", owner=owner),
        data={"fiscal_year_start_month": 0},
        partial=True,
    )
    assert not serializer.is_valid()
    assert "fiscal_year_start_month" in serializer.errors
