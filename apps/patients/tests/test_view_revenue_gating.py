from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.billing.services import capture_visit_charges
from apps.clinical.models import PaymentMode, Visit, VisitType
from apps.clinics.models import Clinic, ClinicStaffMembership
from apps.patients.models import PatientClinicRegistration, PatientProfile
from apps.users.models import User, UserType
from apps.users.tokens import issue_tokens


def _authed_client(user) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user)['access']}")
    return client


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@example.com", password="pw", user_type="doctor")


@pytest.fixture
def receptionist(db):
    return User.objects.create_user(
        email="reception@example.com", password="pw", user_type="receptionist"
    )


@pytest.fixture
def clinic_with_revenue(owner, receptionist):
    clinic = Clinic.objects.create(name="Sharma Clinic", owner=owner)
    ClinicStaffMembership.objects.create(clinic=clinic, user=owner, role=UserType.CLINIC_ADMIN)
    ClinicStaffMembership.objects.create(
        clinic=clinic, user=receptionist, role=UserType.RECEPTIONIST
    )
    patient = PatientProfile.objects.create(
        user=User.objects.create_user(email="p@example.com", password="pw", user_type="patient")
    )
    PatientClinicRegistration.objects.create(patient=patient, clinic=clinic, registered_by=owner)
    visit = Visit.objects.create(
        patient=patient,
        clinic=clinic,
        doctor=owner,
        visit_type=VisitType.CONSULTATION,
        chief_complaint="Cough",
        diagnosis="Cold",
        amount_paid=Decimal("750.00"),
        payment_mode=PaymentMode.CASH,
    )
    # Revenue only lands on the ledger through the real capture path —
    # the view calls this on every visit create/update; this fixture
    # creates the Visit directly via the ORM, so it must call it too.
    capture_visit_charges(visit)
    return clinic


@pytest.mark.django_db
def test_registrations_summary_shows_revenue_to_admin(owner, clinic_with_revenue):
    client = _authed_client(owner)
    response = client.get(
        f"/api/v1/patients/clinic-registrations/?clinic={clinic_with_revenue.external_id}"
    )
    assert response.status_code == 200
    assert response.data["summary"]["total_revenue"] == Decimal("750.00")


@pytest.mark.django_db
def test_registrations_summary_omits_revenue_for_receptionist(receptionist, clinic_with_revenue):
    client = _authed_client(receptionist)
    response = client.get(
        f"/api/v1/patients/clinic-registrations/?clinic={clinic_with_revenue.external_id}"
    )
    assert response.status_code == 200
    assert "total_revenue" not in response.data["summary"]
    assert "total_patients" in response.data["summary"]
