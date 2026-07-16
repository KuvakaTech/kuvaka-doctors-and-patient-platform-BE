from decimal import Decimal

import pytest
from rest_framework.exceptions import ValidationError

from apps.clinics.models import Clinic
from apps.finance.models import (
    BusinessUnit,
    BusinessUnitType,
    EntryStatus,
    RevenueShareRule,
    RevenueSource,
)
from apps.finance.services import (
    create_manual_entry,
    derive_entry_status,
    get_active_share_rule,
    resolve_entry_attribution,
    update_manual_entry,
)
from apps.patients.models import PatientProfile
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


@pytest.fixture
def business_unit(doctor, clinic):
    return BusinessUnit.objects.create(
        owner=doctor, clinic=clinic, name=clinic.name, unit_type=BusinessUnitType.CLINIC
    )


@pytest.fixture
def patient_profile(db):
    user = User.objects.create_user(
        email="patient@example.com", password="pw", user_type="patient"
    )
    return PatientProfile.objects.create(user=user)


# ---------------------------------------------------------------------------
# derive_entry_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "received,total,expected",
    [
        (Decimal("0"), Decimal("500"), EntryStatus.PENDING),
        (Decimal("250"), Decimal("500"), EntryStatus.PARTIAL),
        (Decimal("500"), Decimal("500"), EntryStatus.RECEIVED),
    ],
)
def test_derive_entry_status(received, total, expected):
    assert derive_entry_status(total, received) == expected


# ---------------------------------------------------------------------------
# resolve_entry_attribution
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_resolve_attribution_derives_clinic_from_business_unit(business_unit, clinic):
    assert resolve_entry_attribution(business_unit=business_unit, clinic=None) == clinic


@pytest.mark.django_db
def test_resolve_attribution_rejects_mismatched_explicit_clinic(doctor, business_unit):
    other_clinic = Clinic.objects.create(name="Other Clinic", owner=doctor)
    with pytest.raises(ValidationError):
        resolve_entry_attribution(business_unit=business_unit, clinic=other_clinic)


@pytest.mark.django_db
def test_resolve_attribution_keeps_bare_clinic_with_no_unit(clinic):
    assert resolve_entry_attribution(business_unit=None, clinic=clinic) == clinic


@pytest.mark.django_db
def test_resolve_attribution_none_when_nothing_given():
    assert resolve_entry_attribution(business_unit=None, clinic=None) is None


# ---------------------------------------------------------------------------
# get_active_share_rule
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_share_rule_returns_none(clinic, other_doctor):
    assert get_active_share_rule(clinic, other_doctor) is None


@pytest.mark.django_db
def test_disabled_share_rule_returns_none(clinic, other_doctor):
    RevenueShareRule.objects.create(
        clinic=clinic, doctor=other_doctor, doctor_share_percentage=60, enabled=False
    )
    assert get_active_share_rule(clinic, other_doctor) is None


@pytest.mark.django_db
def test_owner_conducting_own_visit_never_splits(clinic, doctor):
    # Even if a rule somehow existed for the owner themselves, splitting
    # with yourself is defined as a no-op — get_active_share_rule
    # short-circuits before even querying.
    RevenueShareRule.objects.create(
        clinic=clinic, doctor=doctor, doctor_share_percentage=50, enabled=True
    )
    assert get_active_share_rule(clinic, doctor) is None


@pytest.mark.django_db
def test_enabled_share_rule_returned(clinic, other_doctor):
    rule = RevenueShareRule.objects.create(
        clinic=clinic, doctor=other_doctor, doctor_share_percentage=60, enabled=True
    )
    assert get_active_share_rule(clinic, other_doctor) == rule


# ---------------------------------------------------------------------------
# create_manual_entry / update_manual_entry
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_manual_entry_defaults_to_pending_when_no_amount_received(doctor):
    entry = create_manual_entry(
        doctor=doctor,
        validated_data={"source_type": RevenueSource.HOME_VISIT, "amount": Decimal("300.00")},
    )
    assert entry.status == EntryStatus.PENDING
    assert entry.amount_received == Decimal("0")
    assert entry.recorded_by_id == doctor.id
    assert entry.currency == "INR"


@pytest.mark.django_db
def test_create_manual_entry_rejects_received_over_amount(doctor):
    with pytest.raises(ValidationError):
        create_manual_entry(
            doctor=doctor,
            validated_data={
                "source_type": RevenueSource.HOME_VISIT,
                "amount": Decimal("300.00"),
                "amount_received": Decimal("400.00"),
            },
        )


@pytest.mark.django_db
def test_create_manual_entry_derives_clinic_from_unit(doctor, business_unit, clinic):
    entry = create_manual_entry(
        doctor=doctor,
        validated_data={
            "source_type": RevenueSource.CLINIC_PROCEDURE,
            "amount": Decimal("100.00"),
            "business_unit": business_unit,
        },
    )
    assert entry.clinic_id == clinic.id


@pytest.mark.django_db
def test_update_manual_entry_settles_partial_payment(doctor):
    entry = create_manual_entry(
        doctor=doctor,
        validated_data={
            "source_type": RevenueSource.INSURANCE_CLAIM,
            "amount": Decimal("10000.00"),
        },
    )
    updated = update_manual_entry(entry, {"amount_received": Decimal("4000.00")})
    assert updated.status == EntryStatus.PARTIAL
    assert updated.settled_on is None

    fully_settled = update_manual_entry(updated, {"amount_received": Decimal("10000.00")})
    assert fully_settled.status == EntryStatus.RECEIVED
    assert fully_settled.settled_on is not None


@pytest.mark.django_db
def test_update_manual_entry_reducing_received_clears_settled_on(doctor):
    entry = create_manual_entry(
        doctor=doctor,
        validated_data={
            "source_type": RevenueSource.HOME_VISIT,
            "amount": Decimal("500.00"),
            "amount_received": Decimal("500.00"),
        },
    )
    assert entry.status == EntryStatus.RECEIVED
    assert entry.settled_on is not None

    corrected = update_manual_entry(entry, {"amount_received": Decimal("200.00")})
    assert corrected.status == EntryStatus.PARTIAL
    assert corrected.settled_on is None


@pytest.mark.django_db
def test_update_manual_entry_can_set_terminal_refunded_status(doctor):
    entry = create_manual_entry(
        doctor=doctor,
        validated_data={
            "source_type": RevenueSource.HOME_VISIT,
            "amount": Decimal("500.00"),
            "amount_received": Decimal("500.00"),
        },
    )
    refunded = update_manual_entry(entry, {"status": EntryStatus.REFUNDED})
    assert refunded.status == EntryStatus.REFUNDED
