from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.billing.models import ChargeCategory
from apps.billing.services import (
    capture_charge,
    create_draft_invoice,
    get_or_create_account,
    issue_invoice,
)
from apps.clinics.models import Clinic
from apps.finance.models import RevenueEntry
from apps.patients.models import PatientProfile
from apps.users.models import User


@pytest.fixture
def doctor(db):
    return User.objects.create_user(email="doctor@example.com", password="pw", user_type="doctor")


@pytest.fixture
def clinic(doctor):
    return Clinic.objects.create(name="Sharma Clinic", owner=doctor)


@pytest.fixture
def patient_profile(db):
    user = User.objects.create_user(
        email="patient@example.com", password="pw", user_type="patient"
    )
    return PatientProfile.objects.create(user=user)


@pytest.fixture
def account(patient_profile, clinic):
    return get_or_create_account(patient=patient_profile, clinic=clinic)


def _issued_charge(account, doctor, amount="500.00"):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=[{"type": "base", "amount": amount}],
        performer=doctor,
        recorded_by=doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[charge])
    issue_invoice(invoice, issued_by=doctor)
    return charge


@pytest.mark.django_db
def test_no_drift_when_bridge_is_in_sync(account, doctor):
    _issued_charge(account, doctor)
    out = StringIO()
    call_command("verify_ledger", stdout=out)
    assert "No drift detected" in out.getvalue()


@pytest.mark.django_db
def test_missing_bridge_entry_is_detected(account, doctor):
    charge = _issued_charge(account, doctor)
    RevenueEntry.objects.filter(charge_item=charge).delete()

    out = StringIO()
    with pytest.raises(CommandError, match="1 missing bridge entries"):
        call_command("verify_ledger", stdout=out)
    assert "MISSING bridge entry" in out.getvalue()


@pytest.mark.django_db
def test_amount_mismatch_is_detected(account, doctor):
    charge = _issued_charge(account, doctor, amount="500.00")
    entry = RevenueEntry.objects.get(charge_item=charge)
    entry.amount = Decimal("1.00")
    entry.save(update_fields=["amount"])

    out = StringIO()
    with pytest.raises(CommandError, match="1 amount mismatches"):
        call_command("verify_ledger", stdout=out)
    assert "AMOUNT DRIFT" in out.getvalue()


@pytest.mark.django_db
def test_clinic_filter_scopes_the_check(account, doctor, clinic):
    other_clinic = Clinic.objects.create(name="Other Clinic", owner=doctor)
    charge = _issued_charge(account, doctor)
    RevenueEntry.objects.filter(charge_item=charge).delete()

    out = StringIO()
    call_command("verify_ledger", clinic=str(other_clinic.external_id), stdout=out)
    assert "No drift detected" in out.getvalue()
