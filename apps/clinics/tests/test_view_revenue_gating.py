"""
First real enforcement of PermissionFlag.VIEW_REVENUE (it existed before
but no view ever checked it).
Keys are omitted for non-privileged staff, never a 403 — the rest of each
response is legitimately theirs.
"""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.billing.services import capture_visit_charges
from apps.clinical.models import PaymentMode, Visit, VisitType
from apps.clinics.models import Clinic, ClinicStaffMembership
from apps.patients.models import PatientProfile
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
def receptionist(owner):
    user = User.objects.create_user(
        email="reception@example.com", password="pw", user_type="receptionist"
    )
    return user


@pytest.fixture
def clinic(owner, receptionist):
    clinic = Clinic.objects.create(name="Sharma Clinic", owner=owner)
    ClinicStaffMembership.objects.create(clinic=clinic, user=owner, role=UserType.CLINIC_ADMIN)
    ClinicStaffMembership.objects.create(
        clinic=clinic, user=receptionist, role=UserType.RECEPTIONIST
    )
    patient = PatientProfile.objects.create(
        user=User.objects.create_user(email="p@example.com", password="pw", user_type="patient")
    )
    visit = Visit.objects.create(
        patient=patient,
        clinic=clinic,
        doctor=owner,
        visit_type=VisitType.CONSULTATION,
        chief_complaint="Cough",
        diagnosis="Cold",
        amount_paid=Decimal("1000.00"),
        payment_mode=PaymentMode.CASH,
    )
    # Revenue only lands on the ledger through the real capture path —
    # the view calls this on every visit create/update; this fixture
    # creates the Visit directly via the ORM, so it must call it too.
    capture_visit_charges(visit)
    return clinic


@pytest.mark.django_db
def test_clinic_list_summary_shows_revenue_to_admin(owner, clinic):
    client = _authed_client(owner)
    response = client.get("/api/v1/clinics/")
    assert response.status_code == 200
    assert response.data["summary"]["total_revenue"] == Decimal("1000.00")


@pytest.mark.django_db
def test_clinic_list_summary_omits_revenue_for_receptionist(receptionist, clinic):
    client = _authed_client(receptionist)
    response = client.get("/api/v1/clinics/")
    assert response.status_code == 200
    assert "total_revenue" not in response.data["summary"]
    # Non-revenue figures stay visible — only the revenue key is gated.
    assert "total_clinics" in response.data["summary"]


@pytest.mark.django_db
def test_clinic_detail_monthly_revenue_omitted_for_receptionist(receptionist, clinic):
    client = _authed_client(receptionist)
    response = client.get(f"/api/v1/clinics/{clinic.external_id}/")
    assert response.status_code == 200
    assert "monthly_revenue" not in response.data
    assert "patient_count" in response.data


@pytest.mark.django_db
def test_clinic_detail_monthly_revenue_visible_to_admin(owner, clinic):
    client = _authed_client(owner)
    response = client.get(f"/api/v1/clinics/{clinic.external_id}/")
    assert response.status_code == 200
    assert response.data["monthly_revenue"] == Decimal("1000.00")


@pytest.mark.django_db
def test_dashboard_summary_omits_monthly_revenue_for_receptionist(receptionist, clinic):
    client = _authed_client(receptionist)
    response = client.get("/api/v1/clinics/dashboard/")
    assert response.status_code == 200
    assert "monthly_revenue" not in response.data
    assert "active_visits_today" in response.data


@pytest.mark.django_db
def test_dashboard_summary_shows_monthly_revenue_for_admin(owner, clinic):
    client = _authed_client(owner)
    response = client.get("/api/v1/clinics/dashboard/")
    assert response.status_code == 200
    assert response.data["monthly_revenue"] == Decimal("1000.00")
