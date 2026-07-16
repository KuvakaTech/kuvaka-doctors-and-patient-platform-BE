from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.finance.models import BusinessUnit, FinanceAccessGrant, FinanceGrantStatus, RevenueEntry
from apps.finance.permissions import resolve_finance_viewer, scope_queryset_to_grant
from apps.users.models import User


@pytest.fixture
def doctor(db):
    return User.objects.create_user(email="doctor@example.com", password="pw", user_type="doctor")


@pytest.fixture
def grantee(db):
    return User.objects.create_user(email="grantee@example.com", password="pw", user_type="nurse")


@pytest.fixture
def stranger(db):
    return User.objects.create_user(
        email="stranger@example.com", password="pw", user_type="doctor"
    )


@pytest.mark.django_db
def test_own_data_no_grant_needed(doctor):
    resolved, grant = resolve_finance_viewer(doctor, None)
    assert resolved == doctor
    assert grant is None


@pytest.mark.django_db
def test_non_doctor_cannot_view_own_finance_data():
    patient = User.objects.create_user(email="p@example.com", password="pw", user_type="patient")
    with pytest.raises(PermissionDenied):
        resolve_finance_viewer(patient, None)


@pytest.mark.django_db
def test_no_grant_denies_access(doctor, stranger):
    with pytest.raises(PermissionDenied):
        resolve_finance_viewer(stranger, str(doctor.external_id))


@pytest.mark.django_db
def test_active_grant_permits_access(doctor, grantee):
    FinanceAccessGrant.objects.create(doctor=doctor, grantee=grantee)
    resolved, grant = resolve_finance_viewer(grantee, str(doctor.external_id))
    assert resolved == doctor
    assert grant is not None


@pytest.mark.django_db
def test_revoked_grant_denies_access(doctor, grantee):
    FinanceAccessGrant.objects.create(
        doctor=doctor, grantee=grantee, status=FinanceGrantStatus.REVOKED
    )
    with pytest.raises(PermissionDenied):
        resolve_finance_viewer(grantee, str(doctor.external_id))


@pytest.mark.django_db
def test_expired_grant_denies_access(doctor, grantee):
    FinanceAccessGrant.objects.create(
        doctor=doctor, grantee=grantee, expires_at=timezone.now() - timedelta(days=1)
    )
    with pytest.raises(PermissionDenied):
        resolve_finance_viewer(grantee, str(doctor.external_id))


@pytest.mark.django_db
def test_grant_without_expiry_never_expires(doctor, grantee):
    FinanceAccessGrant.objects.create(doctor=doctor, grantee=grantee, expires_at=None)
    resolved, grant = resolve_finance_viewer(grantee, str(doctor.external_id))
    assert resolved == doctor
    assert grant is not None


@pytest.mark.django_db
def test_scope_queryset_to_grant_narrows_by_clinic(doctor, grantee):
    from apps.clinics.models import Clinic

    clinic_a = Clinic.objects.create(name="A", owner=doctor)
    clinic_b = Clinic.objects.create(name="B", owner=doctor)
    RevenueEntry.objects.create(
        doctor=doctor, clinic=clinic_a, source_type="clinic_visit", amount=100
    )
    RevenueEntry.objects.create(
        doctor=doctor, clinic=clinic_b, source_type="clinic_visit", amount=200
    )

    grant = FinanceAccessGrant.objects.create(doctor=doctor, grantee=grantee, clinic=clinic_a)
    qs = scope_queryset_to_grant(RevenueEntry.objects.filter(doctor=doctor), grant)
    assert qs.count() == 1
    assert qs.first().clinic_id == clinic_a.id


@pytest.mark.django_db
def test_scope_queryset_to_grant_narrows_by_business_unit(doctor, grantee):
    unit_a = BusinessUnit.objects.create(owner=doctor, name="Lab A", unit_type="lab")
    unit_b = BusinessUnit.objects.create(owner=doctor, name="Lab B", unit_type="lab")
    RevenueEntry.objects.create(
        doctor=doctor, business_unit=unit_a, source_type="lab_test", amount=100
    )
    RevenueEntry.objects.create(
        doctor=doctor, business_unit=unit_b, source_type="lab_test", amount=200
    )

    grant = FinanceAccessGrant.objects.create(doctor=doctor, grantee=grantee, business_unit=unit_a)
    qs = scope_queryset_to_grant(RevenueEntry.objects.filter(doctor=doctor), grant)
    assert qs.count() == 1
    assert qs.first().business_unit_id == unit_a.id


@pytest.mark.django_db
def test_scope_queryset_to_grant_no_scope_returns_everything(doctor, grantee):
    RevenueEntry.objects.create(doctor=doctor, source_type="home_visit", amount=100)
    RevenueEntry.objects.create(doctor=doctor, source_type="home_visit", amount=200)

    grant = FinanceAccessGrant.objects.create(doctor=doctor, grantee=grantee)
    qs = scope_queryset_to_grant(RevenueEntry.objects.filter(doctor=doctor), grant)
    assert qs.count() == 2


def test_scope_queryset_to_grant_passthrough_when_no_grant():
    # grant=None (own-data access) — the function must not touch the
    # queryset at all, not even evaluate it.
    sentinel = object()
    assert scope_queryset_to_grant(sentinel, None) is sentinel
