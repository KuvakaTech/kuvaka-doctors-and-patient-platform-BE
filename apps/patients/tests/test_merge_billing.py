"""
merge_patients() extension for money records: RevenueEntry.patient
reassignment and PatientAccount merging.
Pre-existing merge behavior (registrations/consents/family links) is
untested elsewhere already — this file only covers the new money paths.
"""

from decimal import Decimal

import pytest

from apps.billing.models import PatientAccount, Payment
from apps.billing.services import (
    capture_charge,
    create_draft_invoice,
    get_or_create_account,
    issue_invoice,
    post_payment,
)
from apps.clinical.models import PaymentMode
from apps.clinics.models import Clinic
from apps.finance.models import RevenueEntry, RevenueSource
from apps.patients.models import PatientProfile
from apps.patients.services import merge_patients
from apps.users.models import User


@pytest.fixture
def doctor(db):
    return User.objects.create_user(email="doctor@example.com", password="pw", user_type="doctor")


@pytest.fixture
def clinic(doctor):
    return Clinic.objects.create(name="Sharma Clinic", owner=doctor)


@pytest.fixture
def other_clinic(doctor):
    return Clinic.objects.create(name="Other Clinic", owner=doctor)


@pytest.fixture
def primary(db):
    user = User.objects.create_user(
        email="primary@example.com", password="pw", user_type="patient"
    )
    return PatientProfile.objects.create(user=user)


@pytest.fixture
def duplicate(db):
    user = User.objects.create_user(email="dup@example.com", password="pw", user_type="patient")
    return PatientProfile.objects.create(user=user)


@pytest.mark.django_db
def test_merge_reassigns_revenue_entries(primary, duplicate, doctor):
    RevenueEntry.objects.create(
        doctor=doctor,
        patient=duplicate,
        source_type=RevenueSource.HOME_VISIT,
        amount=Decimal("300.00"),
    )
    merge_patients(primary=primary, duplicate=duplicate, merged_by=doctor, reason="dup account")
    entry = RevenueEntry.objects.get()
    assert entry.patient_id == primary.pk


@pytest.mark.django_db
def test_merge_reassigns_account_with_no_collision(primary, duplicate, doctor, clinic):
    dup_account = get_or_create_account(patient=duplicate, clinic=clinic)
    charge = capture_charge(
        account=dup_account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=doctor,
    )

    merge_patients(primary=primary, duplicate=duplicate, merged_by=doctor, reason="dup account")

    dup_account.refresh_from_db()
    charge.refresh_from_db()
    assert dup_account.patient_id == primary.pk  # reassigned in place, not soft-deleted
    assert dup_account.deleted is False
    assert charge.patient_id == primary.pk
    assert PatientAccount.objects.filter(patient=primary, clinic=clinic).count() == 1


@pytest.mark.django_db
def test_merge_folds_colliding_accounts_together(primary, duplicate, doctor, clinic):
    primary_account = get_or_create_account(patient=primary, clinic=clinic)
    dup_account = get_or_create_account(patient=duplicate, clinic=clinic)

    # Money already happened on both sides before the merge is discovered.
    primary_charge = capture_charge(
        account=primary_account,
        category="consultation",
        title="Primary visit",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=doctor,
    )
    dup_charge = capture_charge(
        account=dup_account,
        category="consultation",
        title="Duplicate visit",
        price_components=[{"type": "base", "amount": "300.00"}],
        recorded_by=doctor,
    )
    dup_invoice = issue_invoice(
        create_draft_invoice(account=dup_account, charge_items=[dup_charge]), issued_by=doctor
    )
    post_payment(
        account=dup_account,
        invoice=dup_invoice,
        kind="payment",
        amount=Decimal("300.00"),
        method=PaymentMode.CASH,
        received_by=doctor,
    )
    post_payment(
        account=dup_account,
        kind="advance",
        amount=Decimal("100.00"),
        method=PaymentMode.CASH,
        received_by=doctor,
    )

    merge_patients(primary=primary, duplicate=duplicate, merged_by=doctor, reason="dup account")

    dup_account.refresh_from_db()
    primary_account.refresh_from_db()
    dup_charge.refresh_from_db()
    dup_invoice.refresh_from_db()

    assert dup_account.deleted is True
    assert dup_charge.account_id == primary_account.id
    assert dup_charge.patient_id == primary.pk
    assert dup_invoice.account_id == primary_account.id
    assert dup_invoice.patient_id == primary.pk
    assert (
        Payment.objects.filter(account=primary_account).count() == 2
    )  # the payment + the advance
    assert not Payment.objects.filter(account=dup_account).exists()

    # Advance balance folded in; rollups reflect BOTH sides' money now
    # living on the one surviving account.
    assert primary_account.advance_balance == Decimal("100.00")
    assert primary_account.total_gross == Decimal(
        "800.00"
    )  # 500 (primary's own) + 300 (duplicate's)
    assert primary_charge  # untouched — still belongs to primary_account
    primary_charge.refresh_from_db()
    assert primary_charge.account_id == primary_account.id


@pytest.mark.django_db
def test_merge_only_touches_colliding_clinic_leaves_others_alone(
    primary, duplicate, doctor, clinic, other_clinic
):
    get_or_create_account(patient=primary, clinic=clinic)
    dup_account_same_clinic = get_or_create_account(patient=duplicate, clinic=clinic)
    dup_account_other_clinic = get_or_create_account(patient=duplicate, clinic=other_clinic)

    merge_patients(primary=primary, duplicate=duplicate, merged_by=doctor, reason="dup account")

    dup_account_same_clinic.refresh_from_db()
    dup_account_other_clinic.refresh_from_db()
    assert dup_account_same_clinic.deleted is True  # collision -> folded and retired
    assert dup_account_other_clinic.patient_id == primary.pk  # no collision -> reassigned in place
    assert dup_account_other_clinic.deleted is False
