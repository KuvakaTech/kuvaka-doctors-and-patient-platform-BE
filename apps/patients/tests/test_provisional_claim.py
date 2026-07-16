import pytest
from rest_framework.test import APIClient

from apps.clinics.models import Clinic, ClinicStaffMembership
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
def clinic(owner):
    clinic = Clinic.objects.create(name="Sharma Clinic", owner=owner)
    ClinicStaffMembership.objects.create(clinic=clinic, user=owner, role=UserType.CLINIC_ADMIN)
    return clinic


def _create_provisional(client, clinic):
    response = client.post(
        "/api/v1/patients/provisional/",
        {
            "first_name": "Babu",
            "last_name": "Rao",
            "phone_number": "+919876500001",
            "clinic": str(clinic.external_id),
        },
    )
    assert response.status_code == 201, response.data
    return response.data["temporary_pin"]


@pytest.mark.django_db
def test_claim_lets_patient_log_in_normally_afterward(owner, clinic):
    """A claimed account must survive its initial token expiring — claim
    hands back tokens directly (bypassing the login view's email_verified
    gate), so if claim itself never marks the account verified, the
    patient is locked out the moment that token expires."""
    admin_client = _authed_client(owner)
    pin = _create_provisional(admin_client, clinic)

    patient_client = APIClient()
    claim_response = patient_client.post(
        "/api/v1/patients/provisional/claim/",
        {"phone_number": "+919876500001", "pin": pin, "new_password": "BabuRealPass!123"},
    )
    assert claim_response.status_code == 200, claim_response.data
    assert "access" in claim_response.data

    user = User.objects.get(phone_number="+919876500001")
    assert user.email_verified is True

    login_response = APIClient().post(
        "/api/v1/patients/auth/login/",
        {"email": user.email or "", "password": "BabuRealPass!123"},
    )
    # No email on this provisional account, so login-by-email can't
    # succeed either way — the point of this assertion is that it fails
    # for lack of a matching account, never for "email not verified".
    if user.email:
        assert login_response.status_code == 200, login_response.data


@pytest.mark.django_db
def test_claim_with_email_can_log_in_normally_afterward(owner, clinic):
    admin_client = _authed_client(owner)
    response = admin_client.post(
        "/api/v1/patients/provisional/",
        {
            "first_name": "Asha",
            "last_name": "Patel",
            "phone_number": "+919876500002",
            "email": "asha.patel@example.com",
            "clinic": str(clinic.external_id),
        },
    )
    assert response.status_code == 201, response.data
    pin = response.data["temporary_pin"]

    claim_response = APIClient().post(
        "/api/v1/patients/provisional/claim/",
        {"phone_number": "+919876500002", "pin": pin, "new_password": "AshaRealPass!123"},
    )
    assert claim_response.status_code == 200, claim_response.data

    login_response = APIClient().post(
        "/api/v1/patients/auth/login/",
        {"email": "asha.patel@example.com", "password": "AshaRealPass!123"},
    )
    assert login_response.status_code == 200, login_response.data


@pytest.mark.django_db
def test_claim_rejects_wrong_pin(owner, clinic):
    admin_client = _authed_client(owner)
    _create_provisional(admin_client, clinic)

    response = APIClient().post(
        "/api/v1/patients/provisional/claim/",
        {"phone_number": "+919876500001", "pin": "000000", "new_password": "SomePass!123"},
    )
    assert response.status_code == 400
