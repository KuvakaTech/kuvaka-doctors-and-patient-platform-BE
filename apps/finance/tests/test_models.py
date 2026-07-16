from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.clinics.models import Clinic
from apps.finance.models import (
    BusinessUnit,
    BusinessUnitType,
    FinanceAccessGrant,
    RevenueEntry,
    RevenueShareRule,
    RevenueSource,
)
from apps.users.models import User


@pytest.fixture
def doctor(db):
    return User.objects.create_user(email="doctor@example.com", password="pw", user_type="doctor")


@pytest.fixture
def other_doctor(db):
    return User.objects.create_user(email="other@example.com", password="pw", user_type="doctor")


@pytest.fixture
def clinic(doctor):
    return Clinic.objects.create(name="Sharma Clinic", owner=doctor)


# ---------------------------------------------------------------------------
# BusinessUnit
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_business_unit_unique_per_owner_clinic(doctor, clinic):
    BusinessUnit.objects.create(
        owner=doctor, clinic=clinic, name="A", unit_type=BusinessUnitType.CLINIC
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        BusinessUnit.objects.create(
            owner=doctor, clinic=clinic, name="B", unit_type=BusinessUnitType.CLINIC
        )


@pytest.mark.django_db
def test_business_unit_allows_multiple_units_with_no_clinic(doctor):
    # Only the (owner, clinic) pair is uniqueness-constrained (condition:
    # clinic__isnull=False) — non-clinic units (labs, pharmacies, home
    # visit groupings) have no such limit.
    BusinessUnit.objects.create(owner=doctor, name="My Lab", unit_type="lab")
    BusinessUnit.objects.create(owner=doctor, name="My Other Lab", unit_type="lab")
    assert BusinessUnit.objects.filter(owner=doctor).count() == 2


# ---------------------------------------------------------------------------
# RevenueShareRule
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_share_rule_one_per_clinic_doctor(clinic, other_doctor):
    RevenueShareRule.objects.create(clinic=clinic, doctor=other_doctor, doctor_share_percentage=60)
    with pytest.raises(IntegrityError), transaction.atomic():
        RevenueShareRule.objects.create(
            clinic=clinic, doctor=other_doctor, doctor_share_percentage=70
        )


@pytest.mark.django_db
def test_share_rule_percentage_bounds(clinic, other_doctor):
    RevenueShareRule.objects.create(clinic=clinic, doctor=other_doctor, doctor_share_percentage=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        RevenueShareRule.objects.create(
            clinic=clinic, doctor=other_doctor, doctor_share_percentage=101
        )


# ---------------------------------------------------------------------------
# RevenueEntry
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_revenue_entry_rejects_received_greater_than_amount(doctor):
    with pytest.raises(IntegrityError), transaction.atomic():
        RevenueEntry.objects.create(
            doctor=doctor,
            source_type=RevenueSource.HOME_VISIT,
            amount=Decimal("500.00"),
            amount_received=Decimal("600.00"),
        )


@pytest.mark.django_db
def test_revenue_entry_split_fields_must_travel_together(doctor, other_doctor):
    # split_enabled=True with no owner/percentage — rejected by the
    # revenue_split_fields_consistent CHECK constraint.
    with pytest.raises(IntegrityError), transaction.atomic():
        RevenueEntry.objects.create(
            doctor=doctor,
            source_type=RevenueSource.CLINIC_VISIT,
            amount=Decimal("1000.00"),
            split_enabled=True,
        )


@pytest.mark.django_db
def test_doctor_share_amount_full_when_unsplit(doctor):
    entry = RevenueEntry.objects.create(
        doctor=doctor,
        source_type=RevenueSource.HOME_VISIT,
        amount=Decimal("1000.00"),
        amount_received=Decimal("1000.00"),
    )
    assert entry.doctor_share_amount == Decimal("1000.00")
    assert entry.owner_share_amount == Decimal("0")


@pytest.mark.django_db
def test_share_amounts_sum_to_total_when_split(doctor, other_doctor):
    entry = RevenueEntry.objects.create(
        doctor=other_doctor,
        owner=doctor,
        source_type=RevenueSource.CLINIC_VISIT,
        amount=Decimal("1000.00"),
        amount_received=Decimal("1000.00"),
        split_enabled=True,
        doctor_share_percentage=Decimal("60.00"),
    )
    assert entry.doctor_share_amount == Decimal("600.00")
    assert entry.owner_share_amount == Decimal("400.00")
    # Never independently rounded — always sums exactly to `amount`.
    assert entry.doctor_share_amount + entry.owner_share_amount == entry.amount


@pytest.mark.django_db
def test_share_amounts_sum_correctly_with_odd_percentage(doctor, other_doctor):
    # 33.33% of 100.00 rounds to 33.33; the remainder (66.67) must still
    # sum back to exactly 100.00 — this is why owner_share_amount is
    # `amount - doctor_share_amount`, never its own independent rounding.
    entry = RevenueEntry.objects.create(
        doctor=other_doctor,
        owner=doctor,
        source_type=RevenueSource.CLINIC_VISIT,
        amount=Decimal("100.00"),
        split_enabled=True,
        doctor_share_percentage=Decimal("33.33"),
    )
    assert entry.doctor_share_amount == Decimal("33.33")
    assert entry.owner_share_amount == Decimal("66.67")
    assert entry.doctor_share_amount + entry.owner_share_amount == Decimal("100.00")


# ---------------------------------------------------------------------------
# FinanceAccessGrant
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_finance_access_grant_rejects_self_grant(doctor):
    with pytest.raises(IntegrityError), transaction.atomic():
        FinanceAccessGrant.objects.create(doctor=doctor, grantee=doctor)


@pytest.mark.django_db
def test_finance_access_grant_rejects_dual_scope(doctor, other_doctor, clinic):
    unit = BusinessUnit.objects.create(owner=doctor, name="Lab", unit_type="lab")
    with pytest.raises(IntegrityError), transaction.atomic():
        FinanceAccessGrant.objects.create(
            doctor=doctor, grantee=other_doctor, clinic=clinic, business_unit=unit
        )


@pytest.mark.django_db
def test_finance_access_grant_allows_unscoped(doctor, other_doctor):
    grant = FinanceAccessGrant.objects.create(doctor=doctor, grantee=other_doctor)
    assert grant.clinic is None
    assert grant.business_unit is None
    assert grant.status == "active"
