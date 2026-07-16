from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.billing.models import ChargeItem, ChargeItemStatus, Invoice, InvoiceStatus
from apps.clinics.models import Clinic, ClinicStaffMembership
from apps.finance.models import EntryStatus, RevenueEntry, RevenueSource
from apps.patients.models import ConsentGrant, ConsentGrantStatus, ConsentScope, PatientProfile
from apps.users.models import User, UserType
from apps.users.tokens import issue_tokens


def _authed_client(user) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user)['access']}")
    return client


@pytest.fixture
def doctor(db):
    return User.objects.create_user(email="doc@example.com", password="pw", user_type="doctor")


@pytest.fixture
def clinic(doctor):
    clinic = Clinic.objects.create(name="Sharma Clinic", owner=doctor)
    ClinicStaffMembership.objects.create(clinic=clinic, user=doctor, role=UserType.DOCTOR)
    return clinic


@pytest.fixture
def patient_profile(clinic):
    user = User.objects.create_user(
        email="patient@example.com", password="pw", user_type="patient"
    )
    profile = PatientProfile.objects.create(user=user)
    ConsentGrant.objects.create(
        patient=profile,
        grantee_clinic=clinic,
        scope=[ConsentScope.FULL],
        status=ConsentGrantStatus.ACTIVE,
    )
    return profile


def _visits_url(clinic, patient_profile) -> str:
    return (
        f"/api/v1/clinical/clinics/{clinic.external_id}"
        f"/patients/{patient_profile.external_id}/visits/"
    )


@pytest.mark.django_db
def test_visit_quick_pays_through_billing_and_bridges_to_finance(doctor, clinic, patient_profile):
    client = _authed_client(doctor)
    response = client.post(
        _visits_url(clinic, patient_profile),
        {
            "visit_type": "consultation",
            "chief_complaint": "Fever",
            "diagnosis": "Viral fever",
            "amount_paid": "500.00",
            "payment_mode": "cash",
        },
        format="json",
    )
    assert response.status_code == 201, response.data

    charge = ChargeItem.objects.get(visit__doctor=doctor)
    assert charge.total_amount == Decimal("500.00")
    invoice = Invoice.objects.get(charge_items=charge)
    assert invoice.status == InvoiceStatus.PAID

    # Exactly one bridged RevenueEntry, linked via `charge_item` —
    # billing/quick-pay is the only visit-side money path.
    entry = RevenueEntry.objects.get()
    assert entry.visit is None
    assert entry.charge_item_id == charge.pk
    assert entry.source_type == RevenueSource.CLINIC_VISIT
    assert entry.amount == Decimal("500.00")
    assert entry.amount_received == Decimal("500.00")
    assert entry.status == EntryStatus.RECEIVED


@pytest.mark.django_db
def test_visit_with_no_amount_creates_no_charge(doctor, clinic, patient_profile):
    client = _authed_client(doctor)
    response = client.post(
        _visits_url(clinic, patient_profile),
        {"visit_type": "consultation", "chief_complaint": "Fever", "diagnosis": "Viral fever"},
        format="json",
    )
    assert response.status_code == 201, response.data
    assert not ChargeItem.objects.exists()


@pytest.mark.django_db
def test_patching_amount_after_quick_pay_does_not_rewrite_the_billed_charge(
    doctor, clinic, patient_profile
):
    """Once quick-pay has issued and paid the consult charge, a later PATCH
    to the visit's amount_paid must not silently rewrite an already-billed
    invoice — billing history is corrected through the billing screens
    (cancel + re-issue), not by editing the source visit."""
    client = _authed_client(doctor)
    list_url = _visits_url(clinic, patient_profile)
    create_response = client.post(
        list_url,
        {
            "visit_type": "consultation",
            "chief_complaint": "Fever",
            "diagnosis": "Viral fever",
            "amount_paid": "500.00",
            "payment_mode": "cash",
        },
        format="json",
    )
    visit_external_id = create_response.data["external_id"]

    patch_response = client.patch(
        f"{list_url}{visit_external_id}/", {"amount_paid": "900.00"}, format="json"
    )
    assert patch_response.status_code == 200, patch_response.data

    charge = ChargeItem.objects.get(visit__doctor=doctor)
    assert charge.status == ChargeItemStatus.BILLED
    assert charge.total_amount == Decimal("500.00")
    entry = RevenueEntry.objects.get(charge_item=charge)
    assert entry.amount == Decimal("500.00")
